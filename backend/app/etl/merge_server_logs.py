"""수집 서버의 로그를 로컬 DB 로 병합한다.

    python -m backend.app.etl.merge_server_logs data/subway-server.duckdb \
        [--snapshots 서버스냅샷폴더] [--dry-run]

수집 서버(docs/SERVER_COLLECTION.md)의 DB 에서 의미 있는 것은
train_position_log · arrival_log 두 로그 테이블뿐이다. 정적 테이블
(역 마스터·승하차·혼잡도)은 로컬 배치가 원본이므로 건드리지 않는다.

컬럼은 항상 이름으로 맞춘다. 위치(SELECT *) 기반이면 안 되는 이유가 실재한다:
db._migrate 가 ALTER TABLE ADD COLUMN 으로 컬럼을 맨 뒤에 붙이므로, 마이그레이션을
거친 로컬 DB 와 새로 만든 서버 DB 는 같은 코드가 만들었어도 컬럼 순서가 다르다.
순서가 다른 채 위치 기반 INSERT 를 하면 값이 조용히 옆 컬럼으로 들어간다.
스키마 검사도 같은 이유로 순서 무관(이름→타입)으로 비교한다.

멱등성: 전체 컬럼 기준 EXCEPT 로 이미 있는 행을 제외하고 넣는다. 같은 서버
DB 를 두 번 병합해도 행이 늘지 않는다. collected_at 은 라운드(응답 배치) 단위
시각이라 행을 구분하지 못하지만, 전 컬럼이 같은 행은 같은 API 응답 안의
같은 열차·역·상태 — 별개 관측이 아니라 원천의 중복 반환이므로 접어도 된다.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

import duckdb

from ..config import load_settings
from ..db import connect

logger = logging.getLogger("merge_server_logs")

# 병합 대상. 이 외의 테이블은 서버에 있어도 무시한다.
LOG_TABLES = ("train_position_log", "arrival_log")


def _columns(con: duckdb.DuckDBPyConnection, database: str, table: str) -> list[tuple[str, str]]:
    """(컬럼명, 타입) 목록을 정의 순서대로 돌려준다. 테이블이 없으면 빈 목록."""
    rows = con.execute(
        """
        SELECT column_name, data_type
        FROM duckdb_columns()
        WHERE database_name = ? AND table_name = ?
        ORDER BY column_index
        """,
        [database, table],
    ).fetchall()
    return [(name, dtype) for name, dtype in rows]


def _check_schemas(con: duckdb.DuckDBPyConnection, local_db: str) -> bool:
    """병합할 모든 테이블의 스키마가 로컬과 일치하는지 먼저 전부 확인한다.

    확인과 쓰기를 섞으면 첫 테이블만 들어간 반쪽 병합이 남는다.
    비교는 이름→타입 매핑으로 한다 — 순서 차이는 정상(마이그레이션 흔적)이고,
    이름·타입이 다른 것만 진짜 오염 위험이다.
    """
    ok = True
    for table in LOG_TABLES:
        server_cols = _columns(con, "srv", table)
        if not server_cols:
            logger.warning("서버 DB 에 %s 가 없어 건너뜁니다.", table)
            continue
        local_cols = _columns(con, local_db, table)
        if dict(server_cols) != dict(local_cols):
            logger.error(
                "%s 스키마가 다릅니다 — 병합하지 않습니다.\n  로컬: %s\n  서버: %s",
                table,
                local_cols,
                server_cols,
            )
            ok = False
    return ok


def merge_logs(
    con: duckdb.DuckDBPyConnection, local_db: str, *, dry_run: bool
) -> dict[str, int]:
    """로그 테이블별로 로컬에 없는 행만 삽입하고 테이블별 행 수를 돌려준다."""
    inserted: dict[str, int] = {}
    for table in LOG_TABLES:
        if not _columns(con, "srv", table):
            continue
        cols = ", ".join(f'"{name}"' for name, _ in _columns(con, local_db, table))
        diff = f"SELECT {cols} FROM srv.{table} EXCEPT SELECT {cols} FROM {table}"
        new_rows = con.execute(f"SELECT count(*) FROM ({diff})").fetchone()[0]
        if not dry_run and new_rows:
            con.execute(f"INSERT INTO {table} ({cols}) {diff}")
        inserted[table] = new_rows
    return inserted


def copy_snapshots(src_dir: Path, dest_dir: Path, *, dry_run: bool) -> tuple[int, int]:
    """서버 스냅샷 JSON 을 복사한다. 이미 있는 파일명은 건너뛴다.

    파일명에 (kind, key, timestamp) 가 들어 있어 이름이 같으면 같은 녹화다.
    """
    copied = 0
    skipped = 0
    for src in sorted(src_dir.glob("*.json")):
        dest = dest_dir / src.name
        if dest.exists():
            skipped += 1
            continue
        if not dry_run:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        copied += 1
    return copied, skipped


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("server_db", type=Path, help="서버에서 가져온 DuckDB 파일")
    parser.add_argument("--db", type=Path, default=None, help="로컬 DB 경로 (기본: 설정값)")
    parser.add_argument("--snapshots", type=Path, default=None, help="서버 스냅샷 폴더")
    parser.add_argument("--dry-run", action="store_true", help="쓰지 않고 삽입될 행 수만 보고")
    args = parser.parse_args(argv)

    if not args.server_db.is_file():
        logger.error("서버 DB 파일이 없습니다: %s", args.server_db)
        return 1
    if args.snapshots is not None and not args.snapshots.is_dir():
        logger.error("스냅샷 폴더가 없습니다: %s", args.snapshots)
        return 1

    settings = load_settings()
    local_path = args.db or settings.db_path
    if args.server_db.resolve() == local_path.resolve():
        logger.error("서버 DB 와 로컬 DB 가 같은 파일입니다: %s", local_path)
        return 1

    try:
        con = connect(local_path)
    except duckdb.IOException as exc:
        # DuckDB 는 쓰기 연결을 하나만 허용한다. 앱이 열고 있으면 여기서 막힌다.
        logger.error(
            "%s 를 열 수 없습니다. 앱이 이 DB 를 사용 중이면 먼저 멈춰야 합니다.\n"
            "  docker compose down  (로컬 실행이면 uvicorn 종료)\n"
            "  원본 오류: %s",
            local_path,
            str(exc).strip().replace("\n", " "),
        )
        return 1

    try:
        server_path = str(args.server_db).replace("'", "''")
        con.execute(f"ATTACH '{server_path}' AS srv (READ_ONLY)")
        local_db = con.execute("SELECT current_database()").fetchone()[0]

        if not _check_schemas(con, local_db):
            return 1

        inserted = merge_logs(con, local_db, dry_run=args.dry_run)
        for table, count in inserted.items():
            logger.info(
                "%s: %s%d행 (서버 %d행 중 나머지는 이미 로컬에 있음)",
                table,
                "삽입 예정 " if args.dry_run else "+",
                count,
                con.execute(f"SELECT count(*) FROM srv.{table}").fetchone()[0],
            )

        if args.snapshots is not None:
            copied, skipped = copy_snapshots(
                args.snapshots, settings.snapshot_dir, dry_run=args.dry_run
            )
            logger.info(
                "스냅샷: %s%d개, 이미 있어 건너뜀 %d개",
                "복사 예정 " if args.dry_run else "복사 ",
                copied,
                skipped,
            )
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
