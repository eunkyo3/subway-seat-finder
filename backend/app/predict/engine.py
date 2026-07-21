"""예측 엔진 — 통계와 실시간 신호를 합쳐 이번/다음 열차를 가른다.

    예상혼잡도 = 기준혼잡도(역·요일·시간대 통계)
               × 배차간격 보정   ← 앞 열차와 벌어질수록 승객 누적
               × 시발 보정       ← 중간역 시발 빈 열차면 하향

    추천 = 이번/다음 중 min(예상혼잡도)
           단, |이번 − 다음| < 임계값이면 "비슷함" (과잉추천 방지)

혼잡도 통계가 없는 노선(9호선·광역철도)은 예측하지 않고 그 사실을 명시한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import duckdb

from ..config import PREDICTABLE_LINES
from ..naming import normalize_line
from .baseline import Baseline, get_baseline, day_type_of, grade_of, time_slot_of
from .signals import HeadwaySignal, OriginSignal, headway_factor, origin_factor

VERDICT_TAKE_THIS = "take_this"
VERDICT_TAKE_NEXT = "take_next"
VERDICT_SIMILAR = "similar"


def is_predictable(line: str) -> bool:
    """혼잡도 통계가 존재하는 노선인지. 서울교통공사 1~8호선만 해당한다."""
    return normalize_line(line) in PREDICTABLE_LINES


@dataclass(frozen=True)
class TrainPrediction:
    train_no: str | None
    eta_sec: int | None
    express: bool
    terminal_station: str | None
    baseline_pct: float
    expected_pct: float
    grade: str
    baseline_source: str
    baseline_resolution: str
    headway: HeadwaySignal
    origin: OriginSignal
    reasons: list[str] = field(default_factory=list)

    @property
    def load_factor(self) -> float:
        """기준 대비 배수. 하차궤적을 같은 강도로 이어가려면 이 값이 필요하다."""
        if self.baseline_pct <= 0:
            return 1.0
        return self.expected_pct / self.baseline_pct


def predict_train(
    con: duckdb.DuckDBPyConnection,
    *,
    line: str,
    station: str,
    when: datetime,
    train_no: str | None = None,
    eta_sec: int | None = None,
    express: bool = False,
    terminal_station: str | None = None,
    direction: str | None = None,
    headway_sec: float | None = None,
    stations_since_origin: int | None = None,
    is_mid_line_origin: bool = False,
) -> TrainPrediction:
    """한 열차의 예상 혼잡도를 계산한다."""
    day_type = day_type_of(when)
    slot = time_slot_of(when)
    base: Baseline = get_baseline(con, line, station, day_type, slot, direction)

    head = headway_factor(headway_sec, when.hour)
    origin = origin_factor(stations_since_origin, is_mid_line_origin=is_mid_line_origin)

    expected = base.congestion_pct * head.factor * origin.factor

    reasons: list[str] = []
    if head.available:
        gap_min = (head.headway_sec or 0) / 60
        nominal_min = head.nominal_sec / 60
        if head.factor > 1.02:
            reasons.append(
                f"앞 열차와 {gap_min:.1f}분 간격 (평소 {nominal_min:.1f}분) — 승객이 더 쌓였습니다"
            )
        elif head.factor < 0.98:
            reasons.append(f"앞 열차와 {gap_min:.1f}분 간격으로 촘촘합니다")
    if origin.is_mid_line_origin:
        reasons.append(
            f"중간역 시발 열차 (출발 {origin.stations_since_origin}정거장 전) — 비어서 들어옵니다"
        )
    if base.resolution != "exact":
        reasons.append("이 역·시간대 통계가 없어 인접 범위 평균을 썼습니다")
    if base.is_estimated and base.source != "none":
        reasons.append("공식 혼잡도 파일이 없어 승하차 기반 추정치를 썼습니다")

    return TrainPrediction(
        train_no=train_no,
        eta_sec=eta_sec,
        express=express,
        terminal_station=terminal_station,
        baseline_pct=round(base.congestion_pct, 1),
        expected_pct=round(expected, 1),
        grade=grade_of(expected),
        baseline_source=base.source,
        baseline_resolution=base.resolution,
        headway=head,
        origin=origin,
        reasons=reasons,
    )


@dataclass(frozen=True)
class Recommendation:
    verdict: str
    difference_pct: float
    message: str
    this_train: TrainPrediction
    next_train: TrainPrediction | None


def compare_trains(
    this_train: TrainPrediction,
    next_train: TrainPrediction | None,
    *,
    similar_threshold_pct: float,
) -> Recommendation:
    """이번 열차와 다음 열차 중 무엇을 탈지 정한다.

    차이가 임계값 미만이면 추천하지 않는다. 몇 %p 차이로 열차를 보내라고 하는 건
    예측 정밀도를 넘어서는 조언이고, 사용자를 괜히 기다리게 만든다.
    """
    if next_train is None:
        return Recommendation(
            verdict=VERDICT_TAKE_THIS,
            difference_pct=0.0,
            message="다음 열차 정보가 없어 이번 열차를 기준으로 안내합니다.",
            this_train=this_train,
            next_train=None,
        )

    difference = round(this_train.expected_pct - next_train.expected_pct, 1)
    if abs(difference) < similar_threshold_pct:
        return Recommendation(
            verdict=VERDICT_SIMILAR,
            difference_pct=difference,
            message="이번 열차와 다음 열차가 비슷합니다. 그냥 타세요.",
            this_train=this_train,
            next_train=next_train,
        )

    if difference > 0:
        wait_min = None
        if next_train.eta_sec is not None and this_train.eta_sec is not None:
            wait_min = max((next_train.eta_sec - this_train.eta_sec) // 60, 0)
        wait_text = f" {wait_min}분만 더 기다리면 됩니다." if wait_min else ""
        return Recommendation(
            verdict=VERDICT_TAKE_NEXT,
            difference_pct=difference,
            message=f"다음 열차가 {abs(difference):.0f}%p 더 여유롭습니다.{wait_text}",
            this_train=this_train,
            next_train=next_train,
        )

    return Recommendation(
        verdict=VERDICT_TAKE_THIS,
        difference_pct=difference,
        message=f"이번 열차가 {abs(difference):.0f}%p 더 여유롭습니다. 지금 타세요.",
        this_train=this_train,
        next_train=next_train,
    )
