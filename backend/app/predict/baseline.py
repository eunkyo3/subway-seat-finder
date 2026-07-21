"""기준 혼잡도 조회.

예측 공식의 출발점이다. 역·요일·시간대·방향별 통계값을 돌려주고,
없으면 점점 넓은 범위로 물러나며 폴백한다.

    정확 일치 -> 방향 무시 -> 노선·시간대 평균 -> 노선 평균 -> 0

소스는 official(OA-12928 파일) 이 estimated(승하차 추정) 보다 항상 우선한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

import duckdb

from ..config import SOURCE_NONE, SOURCE_OFFICIAL, SOURCE_PRIORITY
from ..naming import normalize_line, normalize_station

# 서울교통공사 혼잡도 등급 기준(정원 대비 %).
GRADE_THRESHOLDS = ((80.0, "여유"), (130.0, "보통"), (150.0, "혼잡"))
GRADE_WORST = "매우혼잡"


def grade_of(congestion_pct: float) -> str:
    for limit, label in GRADE_THRESHOLDS:
        if congestion_pct < limit:
            return label
    return GRADE_WORST


def day_type_of(when: date | datetime) -> str:
    """요일 구분. 공휴일 달력은 없으므로 요일만으로 판단한다."""
    weekday = when.weekday()
    if weekday == 5:
        return "토요일"
    if weekday == 6:
        return "일요일"
    return "평일"


def time_slot_of(when: time | datetime) -> str:
    """30분 단위 슬롯으로 내림한다. 통계가 30분 간격이라 그 격자에 맞춰야 한다."""
    moment = when.time() if isinstance(when, datetime) else when
    return f"{moment.hour:02d}:{'30' if moment.minute >= 30 else '00'}"


@dataclass(frozen=True)
class Baseline:
    congestion_pct: float
    source: str
    #  exact | direction | line_slot | line | none — 얼마나 물러나 찾았는지
    resolution: str

    @property
    def grade(self) -> str:
        return grade_of(self.congestion_pct)

    @property
    def is_estimated(self) -> bool:
        return self.source != SOURCE_OFFICIAL


def _query_one(con: duckdb.DuckDBPyConnection, sql: str, params: list) -> tuple | None:
    return con.execute(sql, params).fetchone()


def get_baseline(
    con: duckdb.DuckDBPyConnection,
    line: str,
    station: str,
    day_type: str,
    time_slot: str,
    direction: str | None = None,
) -> Baseline:
    """기준 혼잡도를 찾는다. 못 찾으면 점점 넓혀 폴백하고 그 사실을 resolution 에 남긴다."""
    line_norm = normalize_line(line)
    name_norm = normalize_station(station)

    for source in SOURCE_PRIORITY:
        if direction:
            row = _query_one(
                con,
                "SELECT congestion_pct FROM congestion_stat WHERE line=? AND name_norm=?"
                " AND day_type=? AND time_slot=? AND direction=? AND source=?",
                [line_norm, name_norm, day_type, time_slot, direction, source],
            )
            if row:
                return Baseline(float(row[0]), source, "exact")

        # 방향을 몰라도 그 역·시간대의 평균은 쓸 수 있다.
        row = _query_one(
            con,
            "SELECT avg(congestion_pct) FROM congestion_stat WHERE line=? AND name_norm=?"
            " AND day_type=? AND time_slot=? AND source=?",
            [line_norm, name_norm, day_type, time_slot, source],
        )
        if row and row[0] is not None:
            return Baseline(float(row[0]), source, "direction")

        # 그 역 통계가 통째로 없으면 같은 시간대의 노선 평균으로 때운다.
        row = _query_one(
            con,
            "SELECT avg(congestion_pct) FROM congestion_stat WHERE line=?"
            " AND day_type=? AND time_slot=? AND source=?",
            [line_norm, day_type, time_slot, source],
        )
        if row and row[0] is not None:
            return Baseline(float(row[0]), source, "line_slot")

        row = _query_one(
            con,
            "SELECT avg(congestion_pct) FROM congestion_stat WHERE line=? AND source=?",
            [line_norm, source],
        )
        if row and row[0] is not None:
            return Baseline(float(row[0]), source, "line")

    # 9호선·광역철도처럼 통계 자체가 없는 노선이다. 호출한 쪽이 예측 불가로 처리한다.
    return Baseline(0.0, SOURCE_NONE, "none")
