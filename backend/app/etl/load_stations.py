"""역 마스터 적재.

두 소스를 병합하는데, 역할이 다르다.

- SearchSTNBySubwayLineInfo : **척추**. 서비스 노선(LINE_NUM)과 FR_CODE(외선번호)를 준다.
  FR_CODE 는 노선 내 실제 지리적 순서라 '몇 정거장 뒤' 계산의 기준이 된다.
- subwayStationMaster       : **좌표만**. 노선 표기가 물리 선로 기준이라(경부선/경인선/경원선)
  서비스 노선과 어긋난다. 특히 경원선은 청량리 위쪽이 1호선, 아래쪽이 경의중앙선이라
  선로 이름만으로는 노선을 정할 수 없다. 그래서 노선 판단에는 쓰지 않는다.

좌표 조인은 2단계다. 노선+역명으로 먼저 맞추고(같은 이름의 다른 역, 예: 5호선 양평 vs
경의중앙선 양평 을 구분하기 위해), 실패하면 역명만으로 맞춘다(경원선 오분류 흡수).
"""

from __future__ import annotations

import logging
from collections import defaultdict

import duckdb

from ..clients.seoul_open import SeoulOpenClient
from ..db import bulk_insert
from ..naming import (
    normalize_line,
    normalize_station,
    parse_station_order,
    station_candidates,
    station_key,
)

logger = logging.getLogger(__name__)

MASTER_SERVICE = "subwayStationMaster"
STN_INFO_SERVICE = "SearchSTNBySubwayLineInfo"

CoordIndex = tuple[dict[tuple[str, str], tuple[float, float]], dict[str, tuple[float, float]]]

# 같은 이름의 좌표들이 이 각거리(도) 안에 모여 있으면 한 역의 출입구 차이로 본다.
# 실측: 환승역 표기 차이는 최대 0.006도(신촌 ~500m)인 반면,
# 진짜 동명이역인 5호선/경의중앙선 양평은 0.61도, 운정은 0.039도로 확연히 갈린다.
SAME_STATION_DEGREES = 0.02


def _spread(points: list[tuple[float, float]]) -> float:
    lats = [p[0] for p in points]
    lngs = [p[1] for p in points]
    return max(max(lats) - min(lats), max(lngs) - min(lngs))


def build_coord_index(master_rows: list[dict]) -> CoordIndex:
    """좌표 룩업 2종을 만든다: (노선,역명) 정밀 색인과 역명 단독 폴백 색인.

    폴백 색인은 진짜 동명이역(양평·운정)에 엉뚱한 좌표를 붙이지 않도록,
    좌표들이 한 역이라고 볼 만큼 가까이 모인 이름에 대해서만 만든다.
    """
    precise: dict[tuple[str, str], tuple[float, float]] = {}
    by_name: dict[str, list[tuple[float, float]]] = defaultdict(list)

    for row in master_rows:
        lat_raw, lng_raw = row.get("LAT"), row.get("LOT")
        if not lat_raw or not lng_raw:
            continue
        latlng = (float(lat_raw), float(lng_raw))
        line = normalize_line(row.get("ROUTE"))
        for candidate in station_candidates(row.get("BLDN_NM")):
            precise.setdefault((line, candidate), latlng)
            if latlng not in by_name[candidate]:
                by_name[candidate].append(latlng)

    fallback: dict[str, tuple[float, float]] = {}
    ambiguous: list[str] = []
    for name, points in by_name.items():
        if len(points) == 1 or _spread(points) <= SAME_STATION_DEGREES:
            fallback[name] = points[0]
        else:
            # 틀린 좌표를 붙이느니 좌표 없음이 낫다.
            ambiguous.append(name)
    if ambiguous:
        logger.info(
            "동명이역이라 역명 단독 폴백에서 제외: %s", sorted(ambiguous)
        )
    return precise, fallback


def lookup_coord(index: CoordIndex, line: str, raw_name: str) -> tuple[float, float] | None:
    precise, fallback = index
    candidates = station_candidates(raw_name)
    for candidate in candidates:
        hit = precise.get((line, candidate))
        if hit:
            return hit
    for candidate in candidates:
        hit = fallback.get(candidate)
        if hit:
            return hit
    return None


def build_station_rows(stn_rows: list[dict], coord_index: CoordIndex) -> list[tuple]:
    """척추 + 좌표를 병합해 station_master 행을 만든다."""
    lines_per_name: dict[str, set[str]] = defaultdict(set)
    for row in stn_rows:
        lines_per_name[normalize_station(row.get("STATION_NM"))].add(
            normalize_line(row.get("LINE_NUM"))
        )

    # FR_CODE 순으로 정렬해야 seq 가 지리적 순서와 일치한다.
    decorated = []
    for row in stn_rows:
        order = parse_station_order(row.get("FR_CODE"))
        if order is None:
            logger.warning(
                "FR_CODE 파싱 실패로 제외: %s %s (%r)",
                row.get("LINE_NUM"),
                row.get("STATION_NM"),
                row.get("FR_CODE"),
            )
            continue
        decorated.append((normalize_line(row.get("LINE_NUM")), order, row))
    decorated.sort(key=lambda item: (item[0], item[1]))

    seq_counter: dict[str, int] = defaultdict(int)
    rows: list[tuple] = []
    seen: set[str] = set()
    skipped_no_coord: list[str] = []

    for line, _order, row in decorated:
        raw_name = (row.get("STATION_NM") or "").strip()
        name_norm = normalize_station(raw_name)
        key = station_key(line, raw_name)
        if not name_norm or key in seen:
            continue

        latlng = lookup_coord(coord_index, line, raw_name)
        if latlng is None:
            skipped_no_coord.append(f"{line} {raw_name}")
            continue

        seen.add(key)
        seq_counter[line] += 1
        rows.append(
            (
                key,
                (row.get("STATION_CD") or "").strip() or None,
                raw_name,
                name_norm,
                (row.get("STATION_NM_ENG") or "").strip() or None,
                line,
                None,  # subway_id 는 naming.subway_id_from_line 으로 파생
                seq_counter[line],
                # FR_CODE '211-3' 에서 뒤 숫자는 지선 안에서의 순번이고, 앞 숫자가
                # 그 지선이 갈라져 나온 지점이다. 지선을 하나로 묶으려면 앞 숫자를
                # 식별자로 써야 한다. 본선(접미사 없음)은 0.
                _order[1] if _order[2] > 0 else 0,
                latlng[0],
                latlng[1],
                len(lines_per_name[name_norm]) > 1,
            )
        )

    if skipped_no_coord:
        logger.warning(
            "좌표를 찾지 못해 제외한 역 %d개: %s", len(skipped_no_coord), skipped_no_coord
        )
    return rows


def load_stations(con: duckdb.DuckDBPyConnection, client: SeoulOpenClient) -> int:
    master_rows = list(client.fetch_all(MASTER_SERVICE))
    stn_rows = list(client.fetch_all(STN_INFO_SERVICE))
    if not master_rows:
        raise RuntimeError(f"{MASTER_SERVICE} 응답이 비었습니다. 인증키를 확인하세요.")
    if not stn_rows:
        raise RuntimeError(f"{STN_INFO_SERVICE} 응답이 비었습니다. 인증키를 확인하세요.")

    rows = build_station_rows(stn_rows, build_coord_index(master_rows))

    con.execute("DELETE FROM station_master")
    bulk_insert(
        con,
        "station_master",
        [
            "station_key", "station_id", "name", "name_norm", "name_eng", "line",
            "subway_id", "seq", "branch_no", "lat", "lng", "transfer_yn",
        ],
        rows,
    )
    logger.info(
        "station_master 적재 완료: %d행 (척추 %d행, 좌표 소스 %d행)",
        len(rows),
        len(stn_rows),
        len(master_rows),
    )
    return len(rows)
