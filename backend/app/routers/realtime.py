"""실시간 열차 위치·도착 조회.

응답에는 반드시 source(live|replay)와 데이터 나이(ageSec)가 들어간다.
화면이 "지금"인지 "재생"인지 사용자에게 숨기면 안 된다.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from ..config import PREDICTABLE_LINES
from ..naming import normalize_line, normalize_station

router = APIRouter(prefix="/api/realtime", tags=["realtime"])


def _serialize(record: dict) -> dict:
    reception = record.get("reception_dt")
    return {
        "trainNo": record.get("train_no"),
        "line": record.get("line"),
        "subwayId": record.get("subway_id"),
        "stationName": record.get("station_name"),
        "stationNameRaw": record.get("station_name_raw"),
        "stationId": record.get("station_id"),
        "direction": record.get("direction"),
        "express": record.get("express", False),
        "terminalStation": record.get("terminal_station"),
        "positionStatus": record.get("position_status"),
        "etaSec": record.get("eta_sec"),
        "arrivalMessage": record.get("arrival_message"),
        "receptionDt": reception.isoformat() if reception else None,
        "ageSec": record.get("age_sec"),
    }


@router.get("/positions")
def positions(
    request: Request,
    line: str = Query(..., description="노선명 (예: 2호선)"),
) -> dict:
    state = request.app.state.app_state
    line_norm = normalize_line(line)
    result = state.realtime.fetch_positions(line_norm)

    # 지도에 찍으려면 좌표가 필요하다. 역명으로 마스터에 붙인다.
    cur = state.cursor()
    coords = {
        row[0]: {"lat": row[1], "lng": row[2], "seq": row[3]}
        for row in cur.execute(
            "SELECT name_norm, lat, lng, seq FROM station_master WHERE line = ?",
            [line_norm],
        ).fetchall()
    }

    trains = []
    for record in result.records:
        payload = _serialize(record)
        located = coords.get(normalize_station(record.get("station_name")))
        payload["lat"] = located["lat"] if located else None
        payload["lng"] = located["lng"] if located else None
        payload["seq"] = located["seq"] if located else None
        trains.append(payload)

    return {
        "line": line_norm,
        "source": result.source,
        "fetchedAt": result.fetched_at.isoformat(),
        "predictionAvailable": line_norm in PREDICTABLE_LINES,
        "count": len(trains),
        "trains": trains,
    }


@router.get("/arrivals")
def arrivals(
    request: Request,
    station: str = Query(..., description="역명 (예: 강남)"),
) -> dict:
    state = request.app.state.app_state
    result = state.realtime.fetch_arrivals(station)
    return {
        "station": station,
        "source": result.source,
        "fetchedAt": result.fetched_at.isoformat(),
        "count": len(result.records),
        "arrivals": [_serialize(record) for record in result.records],
    }
