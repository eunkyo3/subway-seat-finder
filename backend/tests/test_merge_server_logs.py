"""merge_server_logs — 서버 수집 로그 병합의 멱등성과 안전장치.

병합은 조용히 틀리기 좋은 작업이다: 두 번 돌리면 로그가 두 배가 되거나,
스키마가 어긋난 채 위치 기반 INSERT 로 값이 옆 컬럼에 들어간다.
여기서는 합성 서버 DB 로 그 두 실패를 고정한다.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from backend.app.db import connect
from backend.app.etl.merge_server_logs import main

ARR_COLUMNS = (
    "subway_id", "station_id", "station_name", "train_no", "arrival_eta_sec",
    "arrival_code", "express_yn", "terminal_station", "direction", "collected_at",
)

POS_ROW = (
    "1002", "2345", "0222", "강남", "상선", False, "성수",
    "도착", datetime(2026, 7, 28, 8, 1, 0), datetime(2026, 7, 28, 8, 1, 5, 123456),
)
ARR_ROW = (
    "1002", "0222", "강남", "2345", 120, "1", False, "성수",
    "상선", datetime(2026, 7, 28, 8, 1, 5, 123456),
)


def _make_db(path: Path, *, pos_rows=(), arr_rows=()):
    con = connect(path)
    for row in pos_rows:
        con.execute(
            "INSERT INTO train_position_log VALUES (?,?,?,?,?,?,?,?,?,?)", list(row)
        )
    for row in arr_rows:
        con.execute("INSERT INTO arrival_log VALUES (?,?,?,?,?,?,?,?,?,?)", list(row))
    con.close()


def _counts(path: Path) -> tuple[int, int]:
    con = connect(path, read_only=True)
    try:
        pos = con.execute("SELECT count(*) FROM train_position_log").fetchone()[0]
        arr = con.execute("SELECT count(*) FROM arrival_log").fetchone()[0]
        return pos, arr
    finally:
        con.close()


@pytest.fixture()
def local_db(tmp_path: Path) -> Path:
    path = tmp_path / "local.duckdb"
    _make_db(path, pos_rows=[POS_ROW], arr_rows=[ARR_ROW])
    return path


@pytest.fixture()
def server_db(tmp_path: Path) -> Path:
    """로컬과 1행 겹치고(POS_ROW/ARR_ROW) 새 행이 1개씩 있는 서버 DB."""
    new_pos = POS_ROW[:8] + (datetime(2026, 7, 28, 8, 2, 0), datetime(2026, 7, 28, 8, 2, 5))
    new_arr = ARR_ROW[:9] + (datetime(2026, 7, 28, 8, 2, 5),)
    path = tmp_path / "server.duckdb"
    _make_db(path, pos_rows=[POS_ROW, new_pos], arr_rows=[ARR_ROW, new_arr])
    return path


def test_merge_inserts_only_new_rows(local_db, server_db):
    assert main([str(server_db), "--db", str(local_db)]) == 0
    assert _counts(local_db) == (2, 2)  # 기존 1 + 신규 1, 겹친 행은 중복 삽입 안 됨


def test_merge_is_idempotent(local_db, server_db):
    assert main([str(server_db), "--db", str(local_db)]) == 0
    assert main([str(server_db), "--db", str(local_db)]) == 0
    assert _counts(local_db) == (2, 2)  # 두 번 병합해도 행 수 불변


def test_dry_run_writes_nothing(local_db, server_db):
    assert main([str(server_db), "--db", str(local_db), "--dry-run"]) == 0
    assert _counts(local_db) == (1, 1)


def test_schema_mismatch_refuses_whole_merge(local_db, server_db):
    con = connect(server_db)
    con.execute("ALTER TABLE arrival_log ADD COLUMN extra VARCHAR")
    con.close()
    assert main([str(server_db), "--db", str(local_db)]) == 1
    # 위치 로그 스키마는 정상이지만, 반쪽 병합을 막기 위해 그것도 안 들어가야 한다.
    assert _counts(local_db) == (1, 1)


def test_missing_server_file(tmp_path, local_db):
    assert main([str(tmp_path / "none.duckdb"), "--db", str(local_db)]) == 1


def test_same_file_refused(local_db):
    assert main([str(local_db), "--db", str(local_db)]) == 1


def test_migrated_local_column_order_still_merges(tmp_path, server_db):
    """실제 로컬 DB 재현: arrival_code 도입 전 스키마에 _migrate 가 컬럼을 뒤에 붙인 형태.

    신규 생성 DB(서버)는 arrival_code 가 6번째, 마이그레이션된 DB(로컬)는 맨 뒤다.
    같은 코드가 만든 두 DB 인데 순서가 달라, 순서 민감 비교나 위치 기반 INSERT 는
    이 정상 조합에서 반드시 깨진다 — 병합은 이름 기준이어야 한다.
    """
    path = tmp_path / "migrated.duckdb"
    con = connect(path)
    con.execute("DROP TABLE arrival_log")
    con.execute(
        """
        CREATE TABLE arrival_log (
            subway_id VARCHAR, station_id VARCHAR, station_name VARCHAR,
            train_no VARCHAR, arrival_eta_sec INTEGER, express_yn BOOLEAN,
            terminal_station VARCHAR, direction VARCHAR, collected_at TIMESTAMP
        )
        """
    )
    con.execute("ALTER TABLE arrival_log ADD COLUMN arrival_code VARCHAR")
    con.execute(
        f"INSERT INTO arrival_log ({', '.join(ARR_COLUMNS)}) VALUES (?,?,?,?,?,?,?,?,?,?)",
        list(ARR_ROW),
    )
    con.execute("INSERT INTO train_position_log VALUES (?,?,?,?,?,?,?,?,?,?)", list(POS_ROW))
    con.close()

    assert main([str(server_db), "--db", str(path)]) == 0
    assert _counts(path) == (2, 2)  # 겹친 행은 순서가 달라도 이름으로 맞춰져 중복 삽입 안 됨
    # 값이 옆 컬럼으로 새지 않았는지 — 신규 행의 arrival_code 가 제자리에 있어야 한다.
    con = duckdb.connect(str(path), read_only=True)
    codes = {row[0] for row in con.execute("SELECT arrival_code FROM arrival_log").fetchall()}
    con.close()
    assert codes == {"1"}


def test_snapshot_copy_skips_existing(local_db, server_db, tmp_path, monkeypatch):
    src = tmp_path / "srv_snaps"
    dest = tmp_path / "local_snaps"
    src.mkdir()
    dest.mkdir()
    (src / "position_2호선_1.json").write_text("서버판", encoding="utf-8")
    (src / "position_2호선_2.json").write_text("신규", encoding="utf-8")
    (dest / "position_2호선_1.json").write_text("로컬판", encoding="utf-8")
    monkeypatch.setenv("SUBWAY_SNAPSHOT_DIR", str(dest))

    assert main([str(server_db), "--db", str(local_db), "--snapshots", str(src)]) == 0
    # 이미 있는 파일은 덮어쓰지 않고, 없는 파일만 복사된다.
    assert (dest / "position_2호선_1.json").read_text(encoding="utf-8") == "로컬판"
    assert (dest / "position_2호선_2.json").read_text(encoding="utf-8") == "신규"
