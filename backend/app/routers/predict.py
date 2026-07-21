"""예측·추천 API.

실시간 도착 목록에서 이번/다음 열차를 뽑아 각각 예상 혼잡도를 계산하고,
둘을 비교해 무엇을 탈지 추천한다. 목적지를 주면 착석 타임라인까지 붙인다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request

from ..config import PREDICTABLE_LINES
from ..naming import normalize_direction, normalize_line, normalize_station
from ..predict.engine import (
    TrainPrediction,
    compare_trains,
    is_predictable,
    predict_train,
)
from ..predict.signals import compute_headway_sec, detect_origin
from ..predict.trajectory import build_seat_timeline

router = APIRouter(prefix="/api/predict", tags=["predict"])

# 시발 감지를 위해 열차번호 궤적을 되짚어볼 시간 범위.
ORIGIN_LOOKBACK_HOURS = 3


def _train_payload(prediction: TrainPrediction) -> dict:
    return {
        "trainNo": prediction.train_no,
        "etaSec": prediction.eta_sec,
        "etaMin": None if prediction.eta_sec is None else round(prediction.eta_sec / 60),
        "express": prediction.express,
        "terminalStation": prediction.terminal_station,
        "baselinePct": prediction.baseline_pct,
        "expectedPct": prediction.expected_pct,
        "grade": prediction.grade,
        "baselineSource": prediction.baseline_source,
        "baselineResolution": prediction.baseline_resolution,
        "headway": {
            "available": prediction.headway.available,
            "sec": prediction.headway.headway_sec,
            "nominalSec": prediction.headway.nominal_sec,
            "factor": round(prediction.headway.factor, 3),
        },
        "origin": {
            "midLineOrigin": prediction.origin.is_mid_line_origin,
            "stationsSinceOrigin": prediction.origin.stations_since_origin,
            "factor": round(prediction.origin.factor, 3),
        },
        "reasons": prediction.reasons,
    }


def _terminal_names(cur, line: str) -> set[str]:
    """노선의 양 끝 역. 시발 감지에서 정상 종점 시발을 걸러내는 데 쓴다."""
    rows = cur.execute(
        "SELECT name_norm FROM station_master WHERE line = ? AND branch_no = 0"
        " AND seq IN (SELECT min(seq) FROM station_master WHERE line = ? AND branch_no = 0"
        "  UNION SELECT max(seq) FROM station_master WHERE line = ? AND branch_no = 0)",
        [line, line, line],
    ).fetchall()
    return {row[0] for row in rows}


def _origin_signal(cur, line: str, train_no: str | None) -> tuple[bool, int | None]:
    """축적된 위치 로그로 이 열차가 어디서 처음 나타났는지 본다."""
    if not train_no:
        return False, None
    # INTERVAL 은 파라미터로 못 받으므로 기준 시각을 파이썬에서 계산해 넘긴다.
    cutoff = datetime.now() - timedelta(hours=ORIGIN_LOOKBACK_HOURS)
    history = cur.execute(
        "SELECT station_name, reception_dt FROM train_position_log"
        " WHERE train_no = ? AND reception_dt >= ? ORDER BY reception_dt",
        [train_no, cutoff],
    ).fetchall()
    if not history:
        return False, None

    normalized = [(normalize_station(name), dt) for name, dt in history if name and dt]
    is_mid, first_station = detect_origin(normalized, _terminal_names(cur, line))
    if not is_mid or first_station is None:
        return False, None

    seqs = dict(
        cur.execute(
            "SELECT name_norm, seq FROM station_master WHERE line = ?", [line]
        ).fetchall()
    )
    latest_station = normalized[-1][0]
    if first_station in seqs and latest_station in seqs:
        return True, abs(seqs[latest_station] - seqs[first_station])
    return True, None


@router.get("/station/{station}")
def predict_station(
    request: Request,
    station: str,
    line: str = Query(..., description="노선명 (예: 2호선)"),
    direction: str | None = Query(None, description="상선 / 하선"),
    dest: str | None = Query(None, description="목적지 역명. 주면 착석 타임라인 포함"),
    at: datetime | None = Query(
        None,
        description="기준 시각(ISO). 생략하면 현재. 스냅샷 재생·검증용으로 과거 시각을 넣을 수 있다.",
    ),
) -> dict:
    state = request.app.state.app_state
    cur = state.cursor()
    line_norm = normalize_line(line)
    station_norm = normalize_station(station)
    # 실시간 원본은 '상행'/'내선', 통계는 '상선' 을 쓴다. 비교 전에 같은 말로 만든다.
    direction_norm = normalize_direction(direction) or None

    known = cur.execute(
        "SELECT name FROM station_master WHERE line = ? AND name_norm = ?",
        [line_norm, station_norm],
    ).fetchone()
    if not known:
        raise HTTPException(404, f"{line_norm} 에 '{station}' 역이 없습니다.")

    arrival_result = state.realtime.fetch_arrivals(station)
    candidates = [
        record
        for record in arrival_result.records
        if normalize_line(record.get("line")) == line_norm
        and (not direction_norm or record.get("direction") == direction_norm)
    ]
    candidates.sort(key=lambda r: r.get("eta_sec") or 10**9)

    # 방향을 안 주면 가장 먼저 오는 열차의 방향으로 맞춘다.
    # 이걸 안 하면 상행 열차와 하행 열차를 비교해 "다음 열차가 더 여유롭다"고
    # 추천하게 되는데, 그 열차는 반대 방향으로 간다.
    effective_direction = direction_norm
    if effective_direction is None and candidates:
        effective_direction = candidates[0].get("direction") or None
    if effective_direction:
        candidates = [
            r for r in candidates if r.get("direction") == effective_direction
        ]

    response: dict = {
        "station": known[0],
        "line": line_norm,
        "direction": effective_direction,
        "directionInferred": direction_norm is None and effective_direction is not None,
        "source": arrival_result.source,
        "fetchedAt": arrival_result.fetched_at.isoformat(),
        "predictionAvailable": line_norm in PREDICTABLE_LINES,
        "arrivalCount": len(candidates),
    }

    if not is_predictable(line_norm):
        # 9호선·광역철도는 혼잡도 통계 자체가 없다. 도착 정보만 정직하게 준다.
        response["reason"] = (
            f"{line_norm} 은 공개된 혼잡도 통계가 없어 예측하지 않습니다. "
            "도착 정보만 제공합니다."
        )
        response["arrivals"] = [
            {
                "trainNo": r.get("train_no"),
                "etaSec": r.get("eta_sec"),
                "express": r.get("express"),
                "terminalStation": r.get("terminal_station"),
                "direction": r.get("direction"),
            }
            for r in candidates
        ]
        return response

    if not candidates:
        response["reason"] = "지금 이 역으로 오는 열차 정보가 없습니다."
        response["thisTrain"] = None
        response["nextTrain"] = None
        return response

    now = at or datetime.now()
    eta_pairs = [(r.get("train_no"), r.get("eta_sec")) for r in candidates]

    predictions: list[TrainPrediction] = []
    for record in candidates[:2]:
        train_no = record.get("train_no")
        is_mid_origin, since_origin = _origin_signal(cur, line_norm, train_no)
        predictions.append(
            predict_train(
                cur,
                line=line_norm,
                station=station_norm,
                when=now,
                train_no=train_no,
                eta_sec=record.get("eta_sec"),
                express=bool(record.get("express")),
                terminal_station=record.get("terminal_station"),
                direction=effective_direction or record.get("direction"),
                headway_sec=compute_headway_sec(eta_pairs, train_no),
                stations_since_origin=since_origin,
                is_mid_line_origin=is_mid_origin,
            )
        )

    this_train = predictions[0]
    next_train = predictions[1] if len(predictions) > 1 else None
    recommendation = compare_trains(
        this_train, next_train, similar_threshold_pct=state.settings.similar_threshold_pct
    )

    response["thisTrain"] = _train_payload(this_train)
    response["nextTrain"] = _train_payload(next_train) if next_train else None
    response["recommendation"] = {
        "verdict": recommendation.verdict,
        "differencePct": recommendation.difference_pct,
        "message": recommendation.message,
    }

    if dest:
        timeline = build_seat_timeline(
            cur,
            line_norm,
            station_norm,
            dest,
            departure=now,
            direction=effective_direction,
            load_factor=this_train.load_factor,
        )
        response["timeline"] = {
            "destination": dest,
            "source": timeline.baseline_source,
            "seatFromIndex": timeline.seat_from_index,
            "seatFrom": timeline.seat_from.name if timeline.seat_from else None,
            "seatAfterMinutes": (
                timeline.seat_from.minutes_from_now if timeline.seat_from else None
            ),
            "stops": [
                {
                    "seq": stop.seq,
                    "name": stop.name,
                    "minutesFromNow": stop.minutes_from_now,
                    "timeSlot": stop.time_slot,
                    "congestionPct": stop.congestion_pct,
                    "grade": stop.grade,
                    "seatLikely": stop.seat_likely,
                }
                for stop in timeline.stops
            ],
        }
        if not timeline.stops:
            response["timeline"]["reason"] = (
                f"'{dest}' 까지의 경로를 {line_norm} 본선에서 찾지 못했습니다."
            )

    return response
