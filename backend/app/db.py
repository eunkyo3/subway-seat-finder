"""DuckDB 연결과 스키마.

혼잡도·승하차는 배치로 적재하는 정적 테이블이고,
train_position_log / arrival_log 는 실시간 호출 결과를 축적하는 로그다.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import duckdb
import pandas as pd

SCHEMA_STATEMENTS = (
    # 역 마스터: subwayStationMaster(좌표) + SearchSTNBySubwayLineInfo(코드/영문명) 병합 결과
    """
    CREATE TABLE IF NOT EXISTS station_master (
        station_key   VARCHAR NOT NULL,   -- 정규화된 '노선|역명'
        station_id    VARCHAR,            -- STATION_CD (없을 수 있음)
        name          VARCHAR NOT NULL,
        name_norm     VARCHAR NOT NULL,   -- 괄호/공백 제거 역명
        name_eng      VARCHAR,
        line          VARCHAR NOT NULL,   -- '2호선' 정규 포맷
        subway_id     VARCHAR,            -- 실시간 API 의 1001~ 코드
        seq           INTEGER,            -- 노선 내 순서
        branch_no     INTEGER DEFAULT 0,  -- 0=본선, 그 외=지선이 갈라진 지점 번호(지선 식별자)
        lat           DOUBLE NOT NULL,
        lng           DOUBLE NOT NULL,
        transfer_yn   BOOLEAN DEFAULT FALSE,
        PRIMARY KEY (station_key)
    )
    """,
    # 시간대별 승하차. CardSubwayTime 와이드 포맷을 롱포맷으로 펼친 결과.
    """
    CREATE TABLE IF NOT EXISTS station_flow (
        line        VARCHAR NOT NULL,
        name_norm   VARCHAR NOT NULL,
        use_ym      VARCHAR NOT NULL,   -- 'YYYYMM'
        hour        INTEGER NOT NULL,   -- 0~23
        board_cnt   DOUBLE NOT NULL,
        alight_cnt  DOUBLE NOT NULL,
        PRIMARY KEY (line, name_norm, use_ym, hour)
    )
    """,
    # 혼잡도 기준값. source='official'(OA-12928 파일) 이 'estimated' 보다 우선한다.
    """
    CREATE TABLE IF NOT EXISTS congestion_stat (
        line           VARCHAR NOT NULL,
        name_norm      VARCHAR NOT NULL,
        day_type       VARCHAR NOT NULL,   -- 평일 / 토요일 / 일요일
        direction      VARCHAR NOT NULL,   -- 상선 / 하선 / 전체
        time_slot      VARCHAR NOT NULL,   -- 'HH:MM' 30분 단위
        congestion_pct DOUBLE NOT NULL,
        source         VARCHAR NOT NULL,   -- official | estimated
        PRIMARY KEY (line, name_norm, day_type, direction, time_slot, source)
    )
    """,
    # 실시간 위치 축적 로그. 열차번호 궤적으로 시발 감지·배차간격을 계산한다.
    """
    CREATE TABLE IF NOT EXISTS train_position_log (
        subway_id        VARCHAR,
        train_no         VARCHAR,
        station_id       VARCHAR,
        station_name     VARCHAR,
        direction        VARCHAR,
        express_yn       BOOLEAN,
        terminal_station VARCHAR,
        position_status  VARCHAR,   -- 진입/도착/출발/전역출발
        reception_dt     TIMESTAMP, -- recptnDt, 원천 생성시각
        collected_at     TIMESTAMP  -- 우리가 수집한 시각
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS arrival_log (
        subway_id        VARCHAR,
        station_id       VARCHAR,
        station_name     VARCHAR,
        train_no         VARCHAR,
        arrival_eta_sec  INTEGER,
        express_yn       BOOLEAN,
        terminal_station VARCHAR,
        direction        VARCHAR,
        collected_at     TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pos_train ON train_position_log (train_no, reception_dt)",
    "CREATE INDEX IF NOT EXISTS idx_arr_station ON arrival_log (station_name, collected_at)",
    "CREATE INDEX IF NOT EXISTS idx_flow_station ON station_flow (line, name_norm)",
    "CREATE INDEX IF NOT EXISTS idx_cong_station ON congestion_stat (line, name_norm)",
)


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    """스키마를 생성한다. 여러 번 실행해도 안전하다."""
    for statement in SCHEMA_STATEMENTS:
        con.execute(statement)


def bulk_insert(
    con: duckdb.DuckDBPyConnection, table: str, columns: list[str], rows: list[tuple]
) -> int:
    """행 목록을 한 번에 적재한다.

    executemany 는 행마다 파라미터를 바인딩해서 수만 행이면 분 단위로 느려진다.
    DataFrame 을 등록해 한 문장으로 넣으면 같은 일이 수십 밀리초에 끝난다.
    """
    if not rows:
        return 0
    frame = pd.DataFrame(rows, columns=columns)
    con.register("_bulk_insert_src", frame)
    try:
        con.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) SELECT * FROM _bulk_insert_src"
        )
    finally:
        con.unregister("_bulk_insert_src")
    return len(rows)


def connect(db_path: Path, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """DuckDB 에 연결한다. 쓰기 모드면 스키마를 보장한다."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=read_only)
    if not read_only:
        init_schema(con)
    return con


@contextmanager
def session(db_path: Path, *, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    con = connect(db_path, read_only=read_only)
    try:
        yield con
    finally:
        con.close()
