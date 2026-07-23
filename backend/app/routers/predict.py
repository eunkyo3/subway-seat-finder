"""예측·추천 API.

실시간 도착 목록에서 이번/다음 열차를 뽑아 각각 예상 혼잡도를 계산하고,
둘을 비교해 무엇을 탈지 추천한다. 목적지를 주면 착석 타임라인까지 붙인다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request

from ..config import LOOP_LINES, PREDICTABLE_LINES
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


def _arrival_payloads(candidates: list[dict]) -> list[dict]:
    """예측 없이 도착 정보만 내보낼 때의 직렬화. 예측 불가 분기들이 공유한다."""
    return [
        {
            "trainNo": r.get("train_no"),
            "etaSec": r.get("eta_sec"),
            "express": r.get("express"),
            "terminalStation": r.get("terminal_station"),
            "direction": r.get("direction"),
        }
        for r in candidates
    ]


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
    """노선의 끝 역들. 시발 감지에서 정상 종점 시발을 걸러내는 데 쓴다.

    본선뿐 아니라 지선(성수지선 등)의 양끝도 포함한다. 본선 끝만 보면 지선
    종착역에서 정상 출발한 열차를 중간역 시발로 오인해 혼잡도를 깎게 된다.
    """
    rows = cur.execute(
        "SELECT branch_no, seq, name_norm FROM station_master"
        " WHERE line = ? AND seq IS NOT NULL",
        [line],
    ).fetchall()
    by_branch: dict[int, list[tuple[int, str]]] = {}
    for branch_no, seq, name in rows:
        by_branch.setdefault(branch_no, []).append((seq, name))
    terminals: set[str] = set()
    for members in by_branch.values():
        terminals.add(min(members)[1])
        terminals.add(max(members)[1])
    return terminals


def _origin_signal(
    cur, line: str, train_no: str | None, *, now: datetime
) -> tuple[bool, int | None]:
    """축적된 위치 로그로 이 열차가 어디서 처음 나타났는지 본다.

    기준 시각은 호출자가 준 now(=at 또는 현재)다. at 으로 과거를 재생·검증할 때
    현재 시각을 쓰면 그 시점이 아니라 엉뚱한 시간 창의 로그를 보게 된다.
    """
    if not train_no:
        return False, None
    # INTERVAL 은 파라미터로 못 받으므로 기준 시각을 파이썬에서 계산해 넘긴다.
    # 상한에 1분 여유를 두는 건 원천 recptnDt 가 수집 서버 시계보다 앞서는
    # 경우가 실측되기 때문이다(_age_sec 의 음수 클램프와 같은 이유).
    cutoff = now - timedelta(hours=ORIGIN_LOOKBACK_HOURS)
    upper = now + timedelta(minutes=1)
    history = cur.execute(
        "SELECT station_name, reception_dt FROM train_position_log"
        " WHERE train_no = ? AND reception_dt >= ? AND reception_dt <= ?"
        " ORDER BY reception_dt",
        [train_no, cutoff, upper],
    ).fetchall()
    if not history:
        return False, None

    normalized = [(normalize_station(name), dt) for name, dt in history if name and dt]
    is_mid, first_station = detect_origin(normalized, _terminal_names(cur, line))
    if not is_mid or first_station is None:
        return False, None

    seqs = dict(
        cur.execute(
            "SELECT name_norm, seq FROM station_master"
            " WHERE line = ? AND branch_no = 0 AND seq IS NOT NULL",
            [line],
        ).fetchall()
    )
    latest_station = normalized[-1][0]
    if first_station in seqs and latest_station in seqs:
        distance = abs(seqs[latest_station] - seqs[first_station])
        if line in LOOP_LINES:
            # 순환선은 seq 최소·최대가 이웃이다. 되돌이 지점을 넘은 이동을
            # 한 바퀴 도는 거리로 세면 회복 진행도가 즉시 포화돼 보정이 무효가 된다.
            span = max(seqs.values()) - min(seqs.values()) + 1
            distance = min(distance, span - distance)
        return True, distance
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
    # eta 0(도착 직전)은 가장 가까운 열차다. `or 10**9` 식으로 접으면 0 이
    # falsy 라 가장 먼 열차로 밀리므로, None(미상)만 명시적으로 뒤로 보낸다.
    candidates.sort(
        key=lambda r: r["eta_sec"] if r.get("eta_sec") is not None else float("inf")
    )

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
        response["arrivals"] = _arrival_payloads(candidates)
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
        is_mid_origin, since_origin = _origin_signal(cur, line_norm, train_no, now=now)
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

    if this_train.baseline_resolution == "none":
        # 노선은 예측 대상인데 통계가 한 줄도 없다 — ETL 미실행 상태다.
        # 기준 0% 를 '여유'로 단정해 내보내는 것보다 예측 불가를 명시하는 게 정직하다.
        response["predictionAvailable"] = False
        response["reason"] = (
            "혼잡도 통계가 아직 적재되지 않아 예측할 수 없습니다. "
            "python -m backend.app.etl.run_all 로 배치 적재를 먼저 실행하세요."
        )
        response["arrivals"] = _arrival_payloads(candidates)
        response["thisTrain"] = None
        response["nextTrain"] = None
        return response

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
