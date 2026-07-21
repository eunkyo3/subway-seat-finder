"""역명·노선명 정규화.

세 데이터 소스가 서로 다른 표기를 쓰기 때문에 조인 전에 반드시 정규화해야 한다.
- subwayStationMaster        : ROUTE='2호선',   BLDN_NM='서울역'
- SearchSTNBySubwayLineInfo  : LINE_NUM='01호선', STATION_NM='서울역'
- CardSubwayTime             : SBWY_ROUT_LN_NM='1호선', STTN='서울역'
- 실시간 API                  : statnNm='서울역', subwayId='1001'

또한 역명에는 '서울역(1)', '이수(총신대입구)', '4·19민주묘지' 같은 변형이 섞여 있다.
"""

from __future__ import annotations

import re

# 실시간 API subwayId <-> 정규 노선명
SUBWAY_ID_TO_LINE = {
    "1001": "1호선",
    "1002": "2호선",
    "1003": "3호선",
    "1004": "4호선",
    "1005": "5호선",
    "1006": "6호선",
    "1007": "7호선",
    "1008": "8호선",
    "1009": "9호선",
    "1061": "중앙선",
    "1063": "경의중앙선",
    "1065": "공항철도",
    "1067": "경춘선",
    "1069": "수인분당선",
    "1071": "신분당선",
    "1075": "수인분당선",
    "1077": "신분당선",
    "1081": "경강선",
    "1092": "우이신설선",
    "1093": "서해선",
}

LINE_TO_SUBWAY_ID = {
    line: sid for sid, line in SUBWAY_ID_TO_LINE.items() if sid <= "1009"
}

# '01호선' / '1호선' / '수도권 1호선' 등에서 호선 번호를 뽑는다.
_LINE_NUM_RE = re.compile(r"(\d{1,2})\s*호선")
# 역명 뒤에 붙는 괄호 주석: '서울역(1)', '이수(총신대입구)'
_PAREN_RE = re.compile(r"\([^)]*\)")
# 가운뎃점·공백·하이픈 등 구분자
_SEP_RE = re.compile(r"[\s·\-_/]+")


# 원천 데이터는 물리 노선(운영 주체·선로) 단위로 쪼개져 있는데,
# 실시간 API 와 이용자 인식은 서비스 노선 단위다. 지도·예측은 서비스 노선으로 묶어야 한다.
# 예: subwayStationMaster 의 '1호선'은 서울교통공사 구간 10개 역뿐이고,
#     나머지는 경부선/경인선/경원선/장항선으로 흩어져 있다.
PHYSICAL_TO_SERVICE_LINE = {
    "경부선": "1호선",
    "경인선": "1호선",
    "경원선": "1호선",
    "장항선": "1호선",
    "일산선": "3호선",
    "과천선": "4호선",
    "안산선": "4호선",
    "분당선": "수인분당선",
    "수인선": "수인분당선",
    "경의선": "경의중앙선",
    "중앙선": "경의중앙선",
    "공항철도1호선": "공항철도",
    "인천선": "인천1호선",
    "우이신설경전철": "우이신설선",
    "김포도시철도": "김포골드라인",
    "에버라인선": "용인경전철",
    "의정부선": "의정부경전철",
    # 아래는 값을 바꾸지 않지만, 이름 안의 '1호선'/'2호선'이 서울 호선으로
    # 오인되지 않도록 숫자 규칙보다 먼저 걸러내기 위한 항목이다.
    "인천1호선": "인천1호선",
    "인천2호선": "인천2호선",
    "공항철도1호선": "공항철도",
}


def normalize_line(raw: object) -> str:
    """노선 표기를 서비스 노선명('2호선', '경의중앙선')으로 통일한다.

    '01호선'/'1호선'/'9호선2~3단계'/'7호선(인천)' 처럼 흩어진 표기를 하나로 접고,
    경부선 같은 물리 노선명은 대응하는 서비스 노선으로 매핑한다.

    엑셀에서 읽으면 호선이 문자열이 아니라 정수 1 로 오는 파일이 있어
    (같은 데이터셋의 다른 분기 파일은 '1호선' 문자열이다) 숫자도 받는다.
    """
    if raw is None:
        return ""
    cleaned = _SEP_RE.sub("", _PAREN_RE.sub("", str(raw).strip()))
    if not cleaned:
        return ""

    # 엑셀이 '1호선'을 숫자 1 로 저장한 경우. 한 자리 숫자는 호선 번호로 본다.
    if cleaned.isdigit() and len(cleaned) == 1:
        return f"{int(cleaned)}호선"

    # 명시 매핑이 숫자 규칙을 앞선다. '인천2호선'이 서울 2호선으로 접히면 안 된다.
    mapped = PHYSICAL_TO_SERVICE_LINE.get(cleaned)
    if mapped:
        return mapped

    match = _LINE_NUM_RE.search(cleaned)
    if match:
        return f"{int(match.group(1))}호선"
    return cleaned


def normalize_station(raw: object) -> str:
    """역명을 조인 키로 쓸 수 있게 정규화한다.

    괄호 주석과 구분자를 제거하고, 접미사 '역'을 떼어 표기 흔들림을 흡수한다.
    단 '서울역'처럼 '역'이 이름의 일부인 1글자 잔여는 남긴다.
    """
    if raw is None:
        return ""
    text = _PAREN_RE.sub("", str(raw).strip())
    text = _SEP_RE.sub("", text)
    if len(text) > 2 and text.endswith("역"):
        text = text[:-1]
    return text


# 소스마다 대표 역명이 다른 경우. 괄호 규칙으로는 못 맞추는 것만 최소로 둔다.
STATION_ALIASES = {
    "평택지제": "지제",  # SearchSTNBySubwayLineInfo 는 '평택지제', 좌표 소스는 '지제'
    "자양": "뚝섬유원지",  # 7호선. 척추는 '자양', 좌표 소스는 '뚝섬유원지'를 대표명으로 쓴다
}


def station_candidates(raw: str | None) -> list[str]:
    """한 역명이 가질 수 있는 정규 표기 후보들.

    '자양(뚝섬유원지)' 처럼 소스마다 괄호 안쪽을 대표명으로 쓰는 경우가 있어
    바깥쪽·안쪽을 모두 후보로 낸다. 순서는 우선순위 순이며 중복은 제거된다.
    """
    if not raw:
        return []
    text = raw.strip()
    candidates = [normalize_station(text)]

    for inner in re.findall(r"\(([^)]*)\)", text):
        candidates.append(normalize_station(inner))

    candidates.extend(STATION_ALIASES[c] for c in list(candidates) if c in STATION_ALIASES)

    seen: set[str] = set()
    ordered = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def parse_station_order(fr_code: str | None) -> tuple[str, int, int] | None:
    """FR_CODE(외선번호)를 정렬 가능한 키로 바꾼다.

    '133' -> ('', 133, 0)        본선
    '211-3' -> ('', 211, 3)      지선(성수지선 등)
    'A04' -> ('A', 4, 0)         광역철도 계열
    이 값이 노선 내 실제 지리적 순서라, 정거장 수 계산의 기준이 된다.
    """
    if not fr_code:
        return None
    match = re.match(r"^([A-Za-z]*)(\d+)(?:-(\d+))?", fr_code.strip())
    if not match:
        return None
    return (match.group(1).upper(), int(match.group(2)), int(match.group(3) or 0))


def station_key(line: str | None, station: str | None) -> str:
    """station_master 의 기본키."""
    return f"{normalize_line(line)}|{normalize_station(station)}"


# 방향 표기도 소스마다 다르다. 실시간 API 는 updnLine 코드("0"/"1")나 '상행'/'하행',
# 혼잡도 통계(OA-12928)는 '상선'/'하선', 2호선은 '내선'/'외선'을 쓴다.
# 조인과 필터가 되려면 하나로 모아야 한다. 기준은 혼잡도 통계 쪽 어휘다.
DIRECTION_UP = "상선"
DIRECTION_DOWN = "하선"

_UP_TOKENS = frozenset({"0", "상행", "상선", "내선", "상"})
_DOWN_TOKENS = frozenset({"1", "하행", "하선", "외선", "하"})


def normalize_direction(raw: object) -> str:
    """방향 표기를 '상선'/'하선' 으로 통일한다. 알 수 없으면 빈 문자열."""
    text = _SEP_RE.sub("", str(raw or "").strip())
    if not text:
        return ""
    if text in _UP_TOKENS:
        return DIRECTION_UP
    if text in _DOWN_TOKENS:
        return DIRECTION_DOWN
    # '내선순환'/'외선순환' 처럼 꼬리가 붙는 경우가 있다.
    if text.startswith(("상", "내")):
        return DIRECTION_UP
    if text.startswith(("하", "외")):
        return DIRECTION_DOWN
    return ""


def line_from_subway_id(subway_id: str | None) -> str:
    return SUBWAY_ID_TO_LINE.get(str(subway_id or "").strip(), "")


def subway_id_from_line(line: str | None) -> str | None:
    return LINE_TO_SUBWAY_ID.get(normalize_line(line))
