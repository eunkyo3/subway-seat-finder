"""하차 궤적과 착석 타이밍.

"몇 정거장 뒤에 앉을 수 있나"에 답한다. 목적지까지 정거장을 하나씩 밟으며
그 지점의 예상 혼잡도를 뽑고, 좌석이 빌 만큼 내려간 첫 지점을 찾는다.

시간도 같이 흐른다. 10정거장이면 20분쯤 걸리므로 뒤쪽 정거장은 기준 혼잡도를
그 시각의 통계에서 읽어야 한다. 이걸 빼먹으면 퇴근 첨두에 탄 열차가 계속
첨두 혼잡도인 채로 남는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import duckdb

from ..config import LOOP_LINES, best_source
from ..naming import normalize_line, normalize_station
from .baseline import Baseline, get_baseline, day_type_of, grade_of, time_slot_of

# 역간 표준 소요시간. 정차시간을 포함한 대략치다.
SECONDS_PER_STATION = 120

# 이 아래로 내려가면 좌석이 나기 시작한다고 본다. 혼잡도 100% = 정원(좌석+입석)이고,
# 좌석만 채운 상태가 대략 35~40% 수준이다.
SEAT_AVAILABLE_PCT = 45.0


@dataclass(frozen=True)
class TimelineStop:
    seq: int
    name: str
    station_key: str
    minutes_from_now: int
    time_slot: str
    congestion_pct: float
    grade: str
    seat_likely: bool


@dataclass(frozen=True)
class SeatTimeline:
    stops: list[TimelineStop]
    #  좌석이 날 것으로 보는 첫 정거장의 인덱스. 끝까지 안 나면 None.
    seat_from_index: int | None
    baseline_source: str

    @property
    def seat_from(self) -> TimelineStop | None:
        if self.seat_from_index is None:
            return None
        return self.stops[self.seat_from_index]


def _path(
    con: duckdb.DuckDBPyConnection, line: str, origin: str, destination: str
) -> list[tuple[int, str, str]]:
    """출발역에서 목적지까지 지나는 역들을 순서대로 돌려준다.

    같은 본선 위에 있어야 하고(지선 환승은 다루지 않는다), 방향은 seq 대소로 정해진다.

    순환선은 양쪽으로 갈 수 있으므로 정거장 수가 적은 쪽을 고른다. 이걸 안 하면
    2호선 시청->충정로(실제로는 한 정거장)가 노선을 한 바퀴 도는 경로로 나온다.
    """
    line_norm = normalize_line(line)
    rows = con.execute(
        "SELECT seq, name, station_key, name_norm FROM station_master"
        " WHERE line = ? AND branch_no = 0 ORDER BY seq",
        [line_norm],
    ).fetchall()
    if not rows:
        return []

    origin_norm = normalize_station(origin)
    dest_norm = normalize_station(destination)
    index = {row[3]: position for position, row in enumerate(rows)}
    start, end = index.get(origin_norm), index.get(dest_norm)
    if start is None or end is None or start == end:
        return []

    if line_norm in LOOP_LINES:
        total = len(rows)
        forward = (end - start) % total
        backward = (start - end) % total
        if forward <= backward:
            span = [rows[(start + step) % total] for step in range(forward + 1)]
        else:
            span = [rows[(start - step) % total] for step in range(backward + 1)]
    else:
        span = rows[start : end + 1] if start < end else rows[end : start + 1][::-1]

    return [(row[0], row[1], row[2]) for row in span]


def build_seat_timeline(
    con: duckdb.DuckDBPyConnection,
    line: str,
    origin: str,
    destination: str,
    *,
    departure: datetime,
    direction: str | None = None,
    load_factor: float = 1.0,
) -> SeatTimeline:
    """출발역→목적지 구간의 예상 혼잡 변화와 착석 시점을 만든다.

    load_factor 는 실시간 보정(배차간격·시발)으로 얻은 배수다. 이 열차가 통계
    평균보다 붐비거나 비어 있다는 정보를 구간 전체에 걸쳐 유지한다.
    """
    stops_meta = _path(con, line, origin, destination)
    if not stops_meta:
        return SeatTimeline([], None, "none")

    day_type = day_type_of(departure)
    stops: list[TimelineStop] = []
    sources: set[str] = set()

    for offset, (seq, name, station_key) in enumerate(stops_meta):
        moment = departure + timedelta(seconds=SECONDS_PER_STATION * offset)
        slot = time_slot_of(moment)
        base: Baseline = get_baseline(con, line, name, day_type, slot, direction)
        sources.add(base.source)
        pct = max(base.congestion_pct * load_factor, 0.0)
        stops.append(
            TimelineStop(
                seq=seq,
                name=name,
                station_key=station_key,
                minutes_from_now=offset * SECONDS_PER_STATION // 60,
                time_slot=slot,
                congestion_pct=round(pct, 1),
                grade=grade_of(pct),
                seat_likely=pct < SEAT_AVAILABLE_PCT,
            )
        )

    # 출발역 자체는 방금 탄 시점이라 착석 후보에서 뺀다.
    seat_index = next(
        (i for i, stop in enumerate(stops) if i > 0 and stop.seat_likely), None
    )
    return SeatTimeline(stops, seat_index, best_source(sources))
