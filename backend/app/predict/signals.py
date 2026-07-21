"""실시간 보정 신호.

통계만으로는 이번 열차와 다음 열차를 가를 수 없다. 둘 다 같은 30분 구간에 들어가
기준 혼잡도가 똑같이 나오기 때문이다. 그래서 실시간에서만 얻을 수 있는 신호로 보정한다.

- **배차간격(headway)**: 앞 열차와 벌어질수록 승객이 더 쌓여 붐빈다.
- **시발(origin)**: 중간역 시발 열차는 텅 빈 채로 들어온다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# 시간대별 기준 배차간격(분). 실측 간격을 이 값과 비교해 상대적으로 얼마나
# 벌어졌는지 본다. 절대 간격이 아니라 '평소보다 벌어졌는가'가 혼잡을 만든다.
NOMINAL_HEADWAY_MIN = {
    5: 8.0, 6: 5.0, 7: 2.8, 8: 2.5, 9: 3.5, 10: 5.0, 11: 5.0,
    12: 5.0, 13: 5.0, 14: 5.0, 15: 4.6, 16: 4.0, 17: 3.0, 18: 2.5,
    19: 3.0, 20: 4.3, 21: 4.6, 22: 5.0, 23: 6.7, 0: 10.0,
}
DEFAULT_NOMINAL_HEADWAY_MIN = 6.0

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


def nominal_headway_sec(hour: int) -> float:
    return NOMINAL_HEADWAY_MIN.get(hour, DEFAULT_NOMINAL_HEADWAY_MIN) * 60.0


def headway_factor(headway_sec: float | None, hour: int) -> HeadwaySignal:
    """배차간격 보정계수. 간격이 길수록 단조 증가한다."""
    nominal = nominal_headway_sec(hour)
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
    """
    if not is_mid_line_origin or stations_since_origin is None:
        return OriginSignal(False, stations_since_origin, 1.0)

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
