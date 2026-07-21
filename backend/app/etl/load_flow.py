"""시간대별 승하차 적재 (CardSubwayTime).

원본은 와이드 포맷이다. 한 행이 (월, 노선, 역) 이고 시간대 48개 컬럼
HR_4_GET_ON_NOPE ... HR_3_GET_OFF_NOPE 이 붙는다. 운행일 기준이라 4시에서
시작해 익일 3시에 끝난다. 이를 (hour, board, alight) 롱포맷으로 펼친다.

값은 **월 합계**다. 일평균이 필요하면 소비하는 쪽에서 해당 월의 일수로 나눈다.
"""

from __future__ import annotations

import logging
import re

import duckdb

from ..clients.seoul_open import SeoulOpenClient
from ..db import bulk_insert
from ..naming import normalize_line, normalize_station

logger = logging.getLogger(__name__)

FLOW_SERVICE = "CardSubwayTime"
_HOUR_COLUMN = re.compile(r"^HR_(\d{1,2})_GET_(ON|OFF)_NOPE$")


def parse_flow_row(row: dict) -> list[tuple]:
    """와이드 행 하나를 시간대별 롱포맷 행들로 펼친다."""
    line = normalize_line(row.get("SBWY_ROUT_LN_NM"))
    name_norm = normalize_station(row.get("STTN"))
    use_ym = (row.get("USE_MM") or "").strip()
    if not line or not name_norm or not use_ym:
        return []

    board: dict[int, float] = {}
    alight: dict[int, float] = {}
    for key, value in row.items():
        match = _HOUR_COLUMN.match(key)
        if not match:
            continue
        hour = int(match.group(1))
        target = board if match.group(2) == "ON" else alight
        try:
            target[hour] = float(value or 0)
        except (TypeError, ValueError):
            target[hour] = 0.0

    return [
        (line, name_norm, use_ym, hour, board.get(hour, 0.0), alight.get(hour, 0.0))
        for hour in sorted(set(board) | set(alight))
    ]


def aggregate_flow_rows(rows: list[tuple]) -> list[tuple]:
    """같은 (노선, 역, 월, 시간)으로 접히는 행들을 합산한다.

    원본은 물리 선로 단위라 한 역이 여러 번 등장한다. 서울역은 '1호선'(서울교통공사
    구간)과 '경부선'(코레일 구간)에 따로 집계되는데, 둘 다 서비스 노선 1호선이므로
    합쳐야 그 역의 실제 이용객이 된다.
    """
    totals: dict[tuple, list[float]] = {}
    for line, name_norm, use_ym, hour, board, alight in rows:
        key = (line, name_norm, use_ym, hour)
        acc = totals.setdefault(key, [0.0, 0.0])
        acc[0] += board
        acc[1] += alight
    return [(*key, acc[0], acc[1]) for key, acc in totals.items()]


def load_flow(
    con: duckdb.DuckDBPyConnection, client: SeoulOpenClient, use_ym: str
) -> int:
    """지정한 월(YYYYMM)의 승하차를 적재한다. 같은 월은 갈아끼운다."""
    parsed: list[tuple] = []
    for raw in client.fetch_all(FLOW_SERVICE, use_ym):
        parsed.extend(parse_flow_row(raw))

    rows = aggregate_flow_rows(parsed)
    if not rows:
        logger.warning("%s 월 승하차 데이터가 없습니다.", use_ym)
        return 0

    con.execute("DELETE FROM station_flow WHERE use_ym = ?", [use_ym])
    inserted = bulk_insert(
        con,
        "station_flow",
        ["line", "name_norm", "use_ym", "hour", "board_cnt", "alight_cnt"],
        rows,
    )
    logger.info("station_flow 적재 완료: %s %d행", use_ym, inserted)
    return inserted


def latest_available_month(client: SeoulOpenClient, probe_months: list[str]) -> str | None:
    """후보 월을 최신순으로 찔러 데이터가 있는 첫 월을 찾는다."""
    for month in probe_months:
        try:
            rows, _ = client.fetch_page(FLOW_SERVICE, 1, 1, month)
        except Exception:  # noqa: BLE001 - 없는 월은 조용히 다음 후보로
            continue
        if rows:
            return month
    return None
