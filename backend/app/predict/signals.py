"""실시간 보정 신호.

통계만으로는 이번 열차와 다음 열차를 가를 수 없다. 둘 다 같은 30분 구간에 들어가
기준 혼잡도가 똑같이 나오기 때문이다. 그래서 실시간에서만 얻을 수 있는 신호로 보정한다.

- **배차간격(headway)**: 앞 열차와 벌어질수록 승객이 더 쌓여 붐빈다.
- **시발(origin)**: 중간역 시발 열차는 텅 빈 채로 들어온다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..naming import normalize_line

# 시간대별 기준 배차간격(분). 실측 간격을 이 값과 비교해 상대적으로 얼마나
# 벌어졌는지 본다. 절대 간격이 아니라 '평소보다 벌어졌는가'가 혼잡을 만든다.
#
# 노선 구분이 없는 정성값이라 아래 NOMINAL_HEADWAY_MIN_BY_LINE 이 우선한다.
# 실측 대조 결과 이 표는 사실상 2호선 시각표였다 — 2호선은 오차 1~13%로 맞았지만
# 배차가 성긴 5·6·7·9호선은 60~79% 벗어났다.
NOMINAL_HEADWAY_MIN = {
    5: 8.0, 6: 5.0, 7: 2.8, 8: 2.5, 9: 3.5, 10: 5.0, 11: 5.0,
    12: 5.0, 13: 5.0, 14: 5.0, 15: 4.6, 16: 4.0, 17: 3.0, 18: 2.5,
    19: 3.0, 20: 4.3, 21: 4.6, 22: 5.0, 23: 6.7, 0: 10.0,
}
DEFAULT_NOMINAL_HEADWAY_MIN = 6.0

# 노선×시간대 실측 기준 배차간격(분). calibrate_headway 가 arrival_log 에서 뽑은
# 중앙값이며, 셀당 표본 30개(MIN_SAMPLES) 이상인 셀만 올렸다. 몇 개 관측의
# 중앙값을 상수로 승격하면 캘리브레이션이 아니라 노이즈 이식이다.
#
# 출처: 2026-07-27~28 평일 수집, 유효 표본 18,421개 / 충분 셀 33개.
# 커버되지 않은 (노선, 시간대)는 NOMINAL_HEADWAY_MIN 으로 폴백한다 —
# 아침 피크(7~9시)와 저녁 일부(20시)만 측정됐고 낮 시간대·주말은 아직 비어 있다.
NOMINAL_HEADWAY_MIN_BY_LINE: dict[str, dict[int, float]] = {
    "1호선": {7: 4.00, 8: 2.50},
    "2호선": {7: 2.83, 8: 2.17, 9: 2.33, 20: 4.00},
    "3호선": {7: 4.50, 8: 3.50, 9: 4.00, 20: 5.00},
    "4호선": {7: 3.50, 8: 3.00, 9: 3.50, 20: 3.50},
    "5호선": {7: 5.00, 8: 3.00, 9: 4.00, 20: 6.00},
    "6호선": {7: 5.00, 8: 4.00, 9: 5.00, 20: 7.00},
    "7호선": {7: 5.00, 8: 4.00, 9: 4.00, 20: 6.00},
    "9호선": {7: 4.67, 8: 4.42, 9: 5.17, 20: 6.46},
    "우이신설선": {7: 2.88, 8: 2.88, 9: 2.88},
}

# 승객 누적은 간격에 비례하지만 완전 비례는 아니다. 간격이 2배여도 혼잡이 2배가
# 되진 않는다(일부는 다음 열차를 기다리거나 다른 경로를 택한다).
HEADWAY_SENSITIVITY = 0.6
HEADWAY_FACTOR_RANGE = (0.7, 1.8)

# 시발 열차는 거의 비어 있고, 몇 정거장 지나며 평상시 수준을 회복한다.
ORIGIN_EMPTY_FACTOR = 0.25
ORIGIN_RECOVERY_STATIONS = 6


@dataclass(frozen=True)
class HeadwaySignal:
    headway_sec: float | None
    nominal_sec: float
    factor: float
    available: bool


def nominal_headway_sec(hour: int, line: str | None = None) -> float:
    """기준 배차간격(초).

    노선별 실측값이 있으면 그것을 쓰고, 없으면 노선 구분 없는 시간대 기본값으로
    폴백한다. 노선을 모르는 호출부(과거 시그니처)도 그대로 동작한다.
    """
    if line:
        by_hour = NOMINAL_HEADWAY_MIN_BY_LINE.get(normalize_line(line))
        if by_hour is not None and hour in by_hour:
            return by_hour[hour] * 60.0
    return NOMINAL_HEADWAY_MIN.get(hour, DEFAULT_NOMINAL_HEADWAY_MIN) * 60.0


def headway_factor(
    headway_sec: float | None, hour: int, line: str | None = None
) -> HeadwaySignal:
    """배차간격 보정계수. 간격이 길수록 단조 증가한다."""
    nominal = nominal_headway_sec(hour, line)
    if headway_sec is None or headway_sec <= 0:
        # 앞 열차를 못 봤으면 보정하지 않는다. 1.0 은 '모름'이지 '정상'이 아니다.
        return HeadwaySignal(None, nominal, 1.0, available=False)

    ratio = headway_sec / nominal
    raw = 1.0 + HEADWAY_SENSITIVITY * (ratio - 1.0)
    clamped = min(max(raw, HEADWAY_FACTOR_RANGE[0]), HEADWAY_FACTOR_RANGE[1])
    return HeadwaySignal(headway_sec, nominal, clamped, available=True)


def compute_headway_sec(
    arrivals: list[tuple[str, datetime | float]], train_no: str
) -> float | None:
    """같은 방향 도착 목록에서 대상 열차와 바로 앞 열차의 간격을 구한다.

    arrivals 는 (열차번호, 도착까지 남은 초) 목록이다. 도착 예정 시각 순으로 정렬해
    대상 열차 바로 앞 항목과의 차이를 쓴다. 앞 열차가 없으면 None.
    """
    ordered = sorted(
        ((no, float(eta)) for no, eta in arrivals if eta is not None),
        key=lambda item: item[1],
    )
    for index, (no, eta) in enumerate(ordered):
        if no != train_no:
            continue
        if index == 0:
            return None
        return eta - ordered[index - 1][1]
    return None


@dataclass(frozen=True)
class OriginSignal:
    is_mid_line_origin: bool
    stations_since_origin: int | None
    factor: float


def origin_factor(
    stations_since_origin: int | None, *, is_mid_line_origin: bool
) -> OriginSignal:
    """시발 보정계수.

    중간역에서 출발한 열차는 비어 있다. 출발 직후가 가장 비고, 정거장을 지날수록
    평상시 수준으로 돌아온다. 정상 종점 시발은 이미 통계에 반영돼 있으므로 보정하지 않는다.

    감지는 됐는데 거리를 모르면(시발역이 지선에 있는 경우 등) 보정하지 않되
    감지 사실은 보존한다. False 로 접으면 API 라벨과 사유 문구까지 사라져,
    실제로 비어서 들어오는 열차가 아무 표시 없이 '정상'으로 나간다.
    """
    if not is_mid_line_origin:
        return OriginSignal(False, stations_since_origin, 1.0)
    if stations_since_origin is None:
        return OriginSignal(True, None, 1.0)

    progress = min(max(stations_since_origin, 0) / ORIGIN_RECOVERY_STATIONS, 1.0)
    factor = ORIGIN_EMPTY_FACTOR + (1.0 - ORIGIN_EMPTY_FACTOR) * progress
    return OriginSignal(True, stations_since_origin, factor)


def detect_origin(
    history: list[tuple[str, datetime]], terminal_names: set[str]
) -> tuple[bool, str | None]:
    """열차번호 궤적에서 시발역을 찾는다.

    history 는 그 열차번호가 관측된 (역명, 시각) 목록이다. 가장 이른 관측 역이
    종점 집합에 없으면 중간역 시발로 본다.

    한계: 회차·입고 열차도 중간역에서 처음 관측될 수 있다. 수집 이력이 짧을 때도
    앞부분이 잘려 시발처럼 보인다. 그래서 이 신호는 보정계수일 뿐 단정이 아니다.
    """
    if not history:
        return False, None
    first_station, _ = min(history, key=lambda item: item[1])
    return first_station not in terminal_names, first_station
