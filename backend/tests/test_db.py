import duckdb
import pytest

from backend.app.db import SCHEMA_STATEMENTS, connect, init_schema, session

EXPECTED_TABLES = {
    "station_master",
    "station_flow",
    "congestion_stat",
    "train_position_log",
    "arrival_log",
}


def _table_names(con: duckdb.DuckDBPyConnection) -> set[str]:
    return {r[0] for r in con.execute("SHOW TABLES").fetchall()}


def test_init_schema_creates_all_tables():
    con = duckdb.connect(":memory:")
    init_schema(con)
    assert EXPECTED_TABLES <= _table_names(con)


def test_init_schema_is_idempotent():
    con = duckdb.connect(":memory:")
    init_schema(con)
    init_schema(con)  # 재실행해도 예외가 없어야 한다
    assert EXPECTED_TABLES <= _table_names(con)


def test_connect_creates_parent_directory(tmp_path):
    db_path = tmp_path / "nested" / "dir" / "subway.duckdb"
    con = connect(db_path)
    try:
        assert db_path.exists()
        assert EXPECTED_TABLES <= _table_names(con)
    finally:
        con.close()


def test_session_closes_connection(tmp_path):
    db_path = tmp_path / "subway.duckdb"
    with session(db_path) as con:
        con.execute("INSERT INTO station_flow VALUES ('2호선','강남','202606',8,100,200)")
    with session(db_path, read_only=True) as con:
        assert con.execute("SELECT count(*) FROM station_flow").fetchone()[0] == 1


def test_station_master_rejects_null_coordinates():
    con = duckdb.connect(":memory:")
    init_schema(con)
    with pytest.raises(duckdb.ConstraintException):
        con.execute(
            "INSERT INTO station_master (station_key, name, name_norm, line, lat, lng)"
            " VALUES ('2호선|강남','강남역','강남','2호선', NULL, 127.0)"
        )


def test_station_master_primary_key_blocks_duplicates():
    con = duckdb.connect(":memory:")
    init_schema(con)
    stmt = (
        "INSERT INTO station_master (station_key, name, name_norm, line, lat, lng)"
        " VALUES ('2호선|강남','강남역','강남','2호선', 37.4, 127.0)"
    )
    con.execute(stmt)
    with pytest.raises(duckdb.ConstraintException):
        con.execute(stmt)


def test_schema_statement_count_matches_tables_and_indexes():
    # DDL 을 지우고도 테스트가 통과하는 일이 없게 개수를 고정한다.
    assert len(SCHEMA_STATEMENTS) == 9


def test_init_schema_adds_arrival_code_to_legacy_table():
    # CREATE IF NOT EXISTS 는 기존 테이블에 새 컬럼을 붙이지 못한다.
    # arrival_code 도입 전에 만들어진 DB 도 행 손실 없이 승격돼야 한다.
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE arrival_log ("
        " subway_id VARCHAR, station_id VARCHAR, station_name VARCHAR,"
        " train_no VARCHAR, arrival_eta_sec INTEGER, express_yn BOOLEAN,"
        " terminal_station VARCHAR, direction VARCHAR, collected_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO arrival_log VALUES"
        " ('1002','x','강남','2101',60,false,'성수','상선',TIMESTAMP '2026-07-22 08:00:00')"
    )

    init_schema(con)

    columns = {r[1] for r in con.execute("PRAGMA table_info('arrival_log')").fetchall()}
    assert "arrival_code" in columns
    # 기존 행은 남고, 복원 불가능한 arrival_code 는 NULL 로 정직하게 비운다.
    assert con.execute(
        "SELECT count(*), max(arrival_code) FROM arrival_log"
    ).fetchone() == (1, None)
