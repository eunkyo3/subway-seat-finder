"""역·노선 조회와 혼잡 히트맵."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from ..config import PREDICTABLE_LINES, best_source
from ..naming import normalize_line, subway_id_from_line

router = APIRouter(prefix="/api", tags=["stations"])

# 히트맵은 운행 시간대만 보여준다. 01~03시는 전 노선이 0 이라 화면만 낭비한다.
HEATMAP_HOURS = list(range(5, 24)) + [0]


@router.get("/health")
def health(request: Request) -> dict:
    state = request.app.state.app_state
    cur = state.cursor()
    stations = cur.execute("SELECT count(*) FROM station_master").fetchone()[0]
    sources = [
        row[0]
        for row in cur.execute(
            "SELECT DISTINCT source FROM congestion_stat ORDER BY source"
        ).fetchall()
    ]
    return {
        "status": "ok",
        "realtimeEnabled": state.settings.realtime_enabled,
        "stations": stations,
        "congestionSources": sources,
        "congestionSource": best_source(sources),
        "dataReady": stations > 0,
    }


@router.get("/lines")
def list_lines(request: Request) -> dict:
    cur = request.app.state.app_state.cursor()
    rows = cur.execute(
        "SELECT line, count(*), min(lat), max(lat), min(lng), max(lng)"
        " FROM station_master GROUP BY line ORDER BY line"
    ).fetchall()
    return {
        "lines": [
            {
                "line": line,
                "subwayId": subway_id_from_line(line),
                "stationCount": count,
                "predictionAvailable": line in PREDICTABLE_LINES,
                "bounds": {"south": s, "north": n, "west": w, "east": e},
            }
            for line, count, s, n, w, e in rows
        ],
        "predictableLines": list(PREDICTABLE_LINES),
    }


@router.get("/stations")
def list_stations(
    request: Request,
    line: str | None = Query(None, description="노선명. 생략하면 전 노선"),
) -> dict:
    cur = request.app.state.app_state.cursor()
    sql = (
        "SELECT station_key, name, name_norm, name_eng, line, seq, branch_no,"
        " lat, lng, transfer_yn FROM station_master"
    )
    params: list = []
    if line:
        sql += " WHERE line = ?"
        params.append(normalize_line(line))
    sql += " ORDER BY line, seq"

    rows = cur.execute(sql, params).fetchall()
    return {
        "count": len(rows),
        "stations": [
            {
                "stationKey": key,
                "name": name,
                "nameNorm": norm,
                "nameEng": eng,
                "line": line_name,
                "seq": seq,
                "branchNo": branch,
                "lat": lat,
                "lng": lng,
                "transfer": bool(transfer),
                "predictionAvailable": line_name in PREDICTABLE_LINES,
            }
            for key, name, norm, eng, line_name, seq, branch, lat, lng, transfer in rows
        ],
    }


@router.get("/heatmap")
def heatmap(
    request: Request,
    line: str = Query(..., description="노선명"),
    day_type: str = Query("평일", alias="dayType"),
    direction: str | None = Query(None),
) -> dict:
    """역 × 시간대 혼잡 히트맵. 방향을 안 주면 방향 평균."""
    state = request.app.state.app_state
    cur = state.cursor()
    line_norm = normalize_line(line)

    slots = [f"{hour:02d}:00" for hour in HEATMAP_HOURS]

    # 역마다 가장 믿을 만한 소스 하나만 쓴다. 공식과 추정치를 함께 평균하면
    # 공식 통계가 추정 오차만큼 오염되는데(강남 08:00 기준 20%p 이상 벌어진다),
    # 겉으로는 그냥 하나의 숫자라 틀렸다는 걸 알아챌 수도 없다.
    # 노선 단위가 아니라 역 단위로 고르는 이유: 공식 통계는 서울교통공사 구간만
    # 다루므로, 같은 1호선이라도 연천처럼 추정치밖에 없는 역이 있다.
    sql = (
        " WITH scoped AS ("
        "   SELECT m.seq, m.name, c.time_slot, c.congestion_pct, c.source, c.name_norm,"
        "          CASE c.source WHEN 'official' THEN 0 ELSE 1 END AS src_rank"
        "   FROM congestion_stat c"
        "   JOIN station_master m ON m.line = c.line AND m.name_norm = c.name_norm"
        "   WHERE c.line = ? AND c.day_type = ? AND c.time_slot IN "
        f"  ({', '.join('?' * len(slots))})"
    )
    params: list = [line_norm, day_type, *slots]
    if direction:
        sql += " AND c.direction = ?"
        params.append(direction)
    sql += (
        " ), best AS ("
        "   SELECT *, min(src_rank) OVER (PARTITION BY name_norm) AS best_rank FROM scoped"
        " )"
        " SELECT seq, name, time_slot, avg(congestion_pct), any_value(source)"
        " FROM best WHERE src_rank = best_rank"
        " GROUP BY seq, name, time_slot ORDER BY seq"
    )

    rows = cur.execute(sql, params).fetchall()
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"{line_norm} 의 {day_type} 혼잡도 데이터가 없습니다.",
        )

    by_station: dict[tuple[int, str], dict[str, float]] = {}
    station_source: dict[tuple[int, str], str] = {}
    for seq, name, slot, pct, source in rows:
        by_station.setdefault((seq, name), {})[slot] = round(float(pct), 1)
        station_source[(seq, name)] = source

    return {
        "line": line_norm,
        "dayType": day_type,
        "direction": direction,
        "slots": slots,
        # 노선 전체를 대표하는 소스. 역마다 다를 수 있어 stations[].source 도 함께 준다.
        "source": best_source(station_source.values()),
        "stations": [
            {
                "seq": seq,
                "name": name,
                "source": station_source[(seq, name)],
                "values": [values.get(slot) for slot in slots],
            }
            for (seq, name), values in sorted(by_station.items())
        ],
    }
