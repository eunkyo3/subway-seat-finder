"""혼잡도 기준값 적재.

경로가 둘이고, 우선순위가 있다.

1. **official** — data/raw/ 에 놓인 OA-12928(서울교통공사 지하철 혼잡도) 파일.
   요일·역·상하선·30분 단위 실제 통계라 예측의 정답에 가장 가깝다.
2. **estimated** — 파일이 없을 때 station_flow(승하차)로부터 추정.
   실측이 아니므로 source='estimated' 로 표시하고, 조회 시 official 이 있으면 밀린다.

추정 모델은 근사이며 가정이 여럿이다(아래 상수 주석 참고). 발표 시 "추정치"임을
반드시 밝혀야 하고, 공식 파일을 넣는 순간 자동으로 official 이 우선한다.
"""

from __future__ import annotations

import logging
import re
from calendar import monthrange
from pathlib import Path

import duckdb
import pandas as pd

from ..config import LOOP_LINES, SOURCE_ESTIMATED, SOURCE_OFFICIAL
from ..db import bulk_insert
from ..naming import normalize_direction, normalize_line, normalize_station

logger = logging.getLogger(__name__)

CONGESTION_COLUMNS = [
    "line", "name_norm", "day_type", "direction", "time_slot", "congestion_pct", "source",
]


def _dedupe(rows: list[tuple]) -> list[tuple]:
    """기본키가 겹치는 행 중 마지막 것만 남긴다.

    원본 파일에 같은 역·시간대가 두 번 들어 있는 경우가 있어, 벌크 삽입 전에
    직접 걸러야 한다(INSERT OR REPLACE 와 달리 벌크 경로는 중복을 안 봐준다).
    """
    unique: dict[tuple, tuple] = {}
    for row in rows:
        unique[row[:5] + (row[6],)] = row
    return list(unique.values())

# --- 추정 모델 가정 -------------------------------------------------------
# 시간대별 편도 운행 횟수(대략). 첨두시간에 배차가 촘촘해진다.
# 승하차 월 합계를 '열차 한 대당'으로 환산할 때 나누는 값이다.
TRAINS_PER_HOUR = {
    4: 4, 5: 8, 6: 14, 7: 22, 8: 24, 9: 18, 10: 12, 11: 12,
    12: 12, 13: 12, 14: 12, 15: 13, 16: 15, 17: 20, 18: 24, 19: 20,
    20: 14, 21: 13, 22: 12, 23: 9, 0: 5, 1: 2, 2: 1, 3: 1,
}
DEFAULT_TRAINS_PER_HOUR = 10

# 승하차 원본은 방향 구분이 없다. 상·하행에 절반씩 흐른다고 본다.
DIRECTION_SHARE = 0.5
# 추정치가 비현실적으로 튀지 않도록 상한을 둔다.
MAX_ESTIMATED_PCT = 200.0

# 절대 인원 환산에는 가정이 너무 많이 겹쳐(정원·배차·방향비율·일수) 스케일이 크게
# 어긋난다. 그래서 모양만 승하차에서 얻고, 크기는 실제 첨두 혼잡도에 맞춰 보정한다.
# 서울교통공사가 공표하는 최혼잡 구간 첨두 혼잡도가 대체로 150~170% 수준이다.
CALIBRATION_PERCENTILE = 99.5
CALIBRATION_TARGET_PCT = 160.0

WEEKDAY = "평일"
DIRECTION_ALL = "전체"
DIRECTION_UP = "상선"
DIRECTION_DOWN = "하선"

# --- 공식 파일 파싱 -------------------------------------------------------
# 같은 데이터셋인데도 분기마다 시간 컬럼 표기가 다르다. 실측된 형태:
#   '5시30분'        (2025년 분기 파일)
#   '05:30~06:00'    (2026년 분기 파일 — 구간 표기)
# 구간 표기는 시작 시각을 슬롯으로 삼는다. 통계 자체가 그 30분 구간의 값이다.
_TIME_PATTERNS = (
    re.compile(r"^(\d{1,2})\s*시\s*(\d{1,2})\s*분$"),
    re.compile(r"^(\d{1,2})\s*:\s*(\d{2})\s*[~\-–]\s*\d{1,2}\s*:\s*\d{2}$"),
    re.compile(r"^(\d{1,2})\s*시\s*(\d{1,2})\s*분\s*[~\-–].*$"),
    re.compile(r"^(\d{1,2})\s*:\s*(\d{2})$"),
    re.compile(r"^(\d{1,2})시$"),
)

_DAY_TYPE_KEYS = ("요일구분", "요일", "구분")
_LINE_KEYS = ("호선", "노선")
_STATION_KEYS = ("출발역", "역명", "역", "지하철역")
_DIRECTION_KEYS = ("상하구분", "상하선", "방향")


def parse_time_column(label: object) -> str | None:
    """시간대 컬럼명을 'HH:MM' 으로 정규화한다. 시간 컬럼이 아니면 None."""
    text = str(label).strip()
    for pattern in _TIME_PATTERNS:
        match = pattern.match(text)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2)) if match.lastindex and match.lastindex >= 2 else 0
            if 0 <= hour <= 24 and 0 <= minute < 60:
                return f"{hour % 24:02d}:{minute:02d}"
    return None


def _find_column(columns: list[str], keys: tuple[str, ...]) -> str | None:
    """메타 컬럼을 이름 키워드로 찾는다. 완전일치를 부분일치보다 우선한다."""
    for key in keys:
        for col in columns:
            if str(col).strip() == key:
                return col
    for key in keys:
        for col in columns:
            if key in str(col):
                return col
    return None


def _normalize_day_type(value: object) -> str:
    text = str(value).strip()
    # '평일'에도 '일'이 들어 있으므로 평일 판정이 먼저 와야 한다.
    if "평" in text:
        return WEEKDAY
    if "토" in text:
        return "토요일"
    if "일" in text or "휴" in text:
        return "일요일"
    return WEEKDAY


def parse_congestion_frame(frame: pd.DataFrame) -> list[tuple]:
    """OA-12928 데이터프레임을 congestion_stat 행으로 변환한다.

    컬럼 순서·개수가 연도마다 달라지므로 이름으로 찾는다.
    """
    columns = [str(c) for c in frame.columns]
    time_columns = [(c, parse_time_column(c)) for c in columns]
    time_columns = [(c, slot) for c, slot in time_columns if slot]
    if not time_columns:
        raise ValueError("시간대 컬럼을 찾지 못했습니다. OA-12928 형식이 맞는지 확인하세요.")

    line_col = _find_column(columns, _LINE_KEYS)
    station_col = _find_column(columns, _STATION_KEYS)
    if not line_col or not station_col:
        raise ValueError(f"호선/역명 컬럼을 찾지 못했습니다. 발견된 컬럼: {columns[:12]}")

    day_col = _find_column(columns, _DAY_TYPE_KEYS)
    dir_col = _find_column(columns, _DIRECTION_KEYS)

    rows: list[tuple] = []
    for record in frame.to_dict("records"):
        line = normalize_line(record.get(line_col))
        name_norm = normalize_station(record.get(station_col))
        if not line or not name_norm:
            continue
        day_type = _normalize_day_type(record.get(day_col)) if day_col else WEEKDAY
        direction = normalize_direction(record.get(dir_col)) or DIRECTION_ALL if dir_col else DIRECTION_ALL

        for column, slot in time_columns:
            value = record.get(column)
            try:
                pct = float(value)
            except (TypeError, ValueError):
                continue
            if pd.isna(pct):
                continue
            rows.append((line, name_norm, day_type, direction, slot, pct, SOURCE_OFFICIAL))
    return rows


def _read_any(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    for encoding in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path.name} 인코딩을 해석하지 못했습니다.")


def find_congestion_files(raw_dir: Path) -> list[Path]:
    if not raw_dir.is_dir():
        return []
    return sorted(
        p
        for p in raw_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".xlsx", ".xls", ".csv"}
        and not p.name.startswith("~$")
    )


def load_official_congestion(con: duckdb.DuckDBPyConnection, raw_dir: Path) -> int:
    """data/raw/ 의 혼잡도 파일을 적재한다. 없으면 0을 반환한다."""
    files = find_congestion_files(raw_dir)
    if not files:
        logger.info("data/raw/ 에 혼잡도 파일이 없습니다. 추정 경로로 넘어갑니다.")
        return 0

    all_rows: list[tuple] = []
    for path in files:
        try:
            frame = _read_any(path)
            rows = parse_congestion_frame(frame)
        except Exception as exc:  # noqa: BLE001 - 형식이 다른 파일은 건너뛰되 이유를 남긴다
            logger.warning("%s 파싱 실패로 건너뜀: %s", path.name, exc)
            continue
        logger.info("%s 에서 %d행 파싱", path.name, len(rows))
        all_rows.extend(rows)

    if not all_rows:
        return 0

    con.execute("DELETE FROM congestion_stat WHERE source = ?", [SOURCE_OFFICIAL])
    inserted = bulk_insert(con, "congestion_stat", CONGESTION_COLUMNS, _dedupe(all_rows))
    logger.info("congestion_stat(official) 적재 완료: %d행", inserted)
    return inserted


def _days_in_month(use_ym: str) -> int:
    try:
        return monthrange(int(use_ym[:4]), int(use_ym[4:6]))[1]
    except (ValueError, IndexError):
        return 30


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(len(ordered) * pct / 100.0), len(ordered) - 1)
    return ordered[index]


def balance_flows(
    stations: list[tuple[str, float, float]]
) -> list[tuple[str, float, float]]:
    """한 시간대 안에서 승차 총합과 하차 총합을 맞춘다.

    원본은 승차 시각과 하차 시각을 각각 그 시각의 시간대에 집계한다. 5시 50분에
    타서 6시 20분에 내리면 승차는 5시대, 하차는 6시대로 흩어진다. 그 결과
    첫차 시간대는 승차만, 막차 시간대는 하차만 잔뜩 잡혀 누적이 한쪽으로 폭주한다.

    한 시간대를 닫힌 계로 보고 하차를 비율 보정하면 이 왜곡이 사라진다.
    """
    total_board = sum(board for _, board, _ in stations)
    total_alight = sum(alight for _, _, alight in stations)
    if total_board <= 0 or total_alight <= 0:
        return stations
    ratio = total_board / total_alight
    return [(name, board, alight * ratio) for name, board, alight in stations]


def directional_profile(
    stations: list[tuple[str, float, float]], descending: bool, *, loop: bool = False
) -> dict[str, float]:
    """한 방향으로 노선을 걸으며 각 역에 **도착하는 열차**의 재차인원을 만든다.

    stations 는 seq 오름차순 (역명, 승차, 하차) 이다.

    앱이 답해야 하는 질문은 "지금 우리 역에 들어오는 열차가 얼마나 붐비나"이므로,
    역 i 의 값은 i 에서의 승하차를 반영하기 **전** 누적값이다.

    선형 노선은 종점에서 열차가 비므로 0 에서 시작하고, 음수로 내려가면 0 에서 자른다
    (열차에 탄 사람이 음수일 수는 없다).

    순환선(2호선)은 기점이 없다. 어디서 시작하든 한 바퀴 돌면 제자리이므로,
    자르는 대신 가장 비는 지점을 0 으로 놓고 전체를 들어올린다. 선형처럼 다루면
    시작점에서 먼 구간일수록 누적이 계속 쌓여 형상이 통째로 망가진다.
    """
    walk = list(reversed(stations)) if descending else stations
    running = 0.0
    profile: dict[str, float] = {}
    for name_norm, board, alight in walk:
        profile[name_norm] = running
        running += (board - alight) * DIRECTION_SHARE
        if not loop:
            running = max(running, 0.0)

    if loop and profile:
        floor = min(profile.values())
        profile = {name: value - floor for name, value in profile.items()}
    return profile


def estimate_congestion(con: duckdb.DuckDBPyConnection) -> int:
    """승하차로 열차 혼잡도를 추정한다.

    노선을 한 방향으로 걸으며 (승차-하차) 를 누적하면 각 역을 통과하는 열차의
    재차인원 **형상**이 나온다. 상·하행을 따로 걷고, 지선은 열차가 따로 다니므로
    독립된 구간으로 걷는다. 절대 크기는 가정이 너무 겹쳐 믿을 수 없으므로
    마지막에 전체를 실제 첨두 혼잡도 범위로 보정한다.

    이 값은 어디까지나 추정이다. data/raw/ 에 공식 혼잡도 파일이 들어오면
    source='official' 이 우선 적용되어 이 추정치는 밀려난다.
    """
    flow = con.execute(
        """
        SELECT f.line, m.branch_no, f.use_ym, f.hour, m.seq, f.name_norm,
               f.board_cnt, f.alight_cnt
        FROM station_flow f
        JOIN station_master m
          ON m.line = f.line AND m.name_norm = f.name_norm
        ORDER BY f.line, m.branch_no, f.use_ym, f.hour, m.seq
        """
    ).fetchall()
    if not flow:
        logger.warning("station_flow 가 비어 있어 혼잡도를 추정할 수 없습니다.")
        return 0

    # 지선(성수지선·신정지선)은 본선과 선로가 갈라져 열차도 따로 다닌다.
    # 같이 누적하면 본선 형상까지 망가지므로 각자 독립된 구간으로 걷는다.
    groups: dict[tuple[str, int, str, int], list[tuple[str, float, float]]] = {}
    for line, branch_no, use_ym, hour, _seq, name_norm, board, alight in flow:
        groups.setdefault((line, int(branch_no), use_ym, int(hour)), []).append(
            (name_norm, float(board), float(alight))
        )

    # 1단계: 보정 전 열차당 재차인원을 모두 계산한다.
    raw: list[tuple[str, str, str, str, float]] = []
    for (line, branch_no, use_ym, hour), stations in groups.items():
        days = _days_in_month(use_ym)
        trains = TRAINS_PER_HOUR.get(hour, DEFAULT_TRAINS_PER_HOUR)
        balanced = balance_flows(stations)
        # 순환 특성은 본선에만 있다. 지선은 왕복하는 짧은 선형 구간이다.
        is_loop = line in LOOP_LINES and branch_no == 0
        for direction, descending in ((DIRECTION_UP, False), (DIRECTION_DOWN, True)):
            profile = directional_profile(balanced, descending, loop=is_loop)
            for name_norm, onboard in profile.items():
                per_train = onboard / days / trains
                raw.append((line, name_norm, direction, f"{hour:02d}", per_train))

    # 2단계: 상위 분위수가 실제 첨두 혼잡도가 되도록 전체 스케일을 맞춘다.
    reference = _percentile([v for *_, v in raw], CALIBRATION_PERCENTILE)
    if reference <= 0:
        logger.warning("추정 기준값이 0 이라 혼잡도를 보정할 수 없습니다.")
        return 0
    scale = CALIBRATION_TARGET_PCT / reference
    logger.info(
        "혼잡도 추정 보정: p%.1f=%.1f명/편성 -> %.0f%% (계수 %.4f)",
        CALIBRATION_PERCENTILE,
        reference,
        CALIBRATION_TARGET_PCT,
        scale,
    )

    rows: list[tuple] = []
    for line, name_norm, direction, hour_text, per_train in raw:
        pct = min(per_train * scale, MAX_ESTIMATED_PCT)
        # 30분 단위 슬롯 두 개에 같은 값을 채운다. 원본이 1시간 단위라 더 못 쪼갠다.
        for minute in ("00", "30"):
            rows.append(
                (
                    line,
                    name_norm,
                    WEEKDAY,
                    direction,
                    f"{hour_text}:{minute}",
                    round(pct, 2),
                    SOURCE_ESTIMATED,
                )
            )

    con.execute("DELETE FROM congestion_stat WHERE source = ?", [SOURCE_ESTIMATED])
    inserted = bulk_insert(con, "congestion_stat", CONGESTION_COLUMNS, _dedupe(rows))
    logger.info("congestion_stat(estimated) 적재 완료: %d행", inserted)
    return inserted


def load_congestion(con: duckdb.DuckDBPyConnection, raw_dir: Path) -> dict[str, int]:
    """공식 파일을 적재하고, 추정치도 항상 함께 만들어 둔다.

    추정치를 항상 만드는 이유: 공식 파일은 서울교통공사 1~8호선만 다루므로
    나머지 구간은 추정치라도 있어야 화면이 비지 않는다. 조회 시 official 이 우선한다.
    """
    official = load_official_congestion(con, raw_dir)
    estimated = estimate_congestion(con)
    if official == 0:
        logger.warning(
            "공식 혼잡도 파일이 없어 추정치만 사용합니다. "
            "OA-12928 파일을 %s 에 넣으면 자동으로 우선 적용됩니다.",
            raw_dir,
        )
    return {SOURCE_OFFICIAL: official, SOURCE_ESTIMATED: estimated}
