"""설정 로딩.

모든 설정은 **환경변수** 하나로 통일한다. 프로젝트 루트의 `.env` 를 읽어
환경변수로 올린 뒤 os.environ 에서만 값을 꺼낸다. 도커든 로컬이든 CI든
설정 주입 경로가 하나라 "어디에 넣어야 하나"를 헷갈릴 일이 없다.

이미 셸/도커가 넣어 준 값은 `.env` 가 덮어쓰지 않는다. 배포 환경의 주입이
파일보다 우선해야 하기 때문이다.

인증키는 두 종류이고 서로 호환되지 않는다.
- SEOUL_API_KEY          : openapi.seoul.go.kr:8088 (역 마스터, 승하차 통계)
- SEOUL_REALTIME_API_KEY : swopenapi.seoul.go.kr (열차 위치, 도착)
  별도 신청 대상이라 없을 수 있다. 없으면 실시간 계층이 replay 로 폴백한다.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
SNAPSHOT_DIR = DATA_DIR / "snapshots"

# 서울교통공사 운영 노선. 혼잡도 통계가 존재하는 범위이자 예측 지원 범위.
PREDICTABLE_LINES = ("1호선", "2호선", "3호선", "4호선", "5호선", "6호선", "7호선", "8호선")

# 본선이 순환하는 노선. 기점이 없어 누적 계산과 경로 탐색이 선형 노선과 다르다.
# 서울에서는 2호선 본선뿐이다(성수·신정 지선은 선형).
LOOP_LINES = frozenset({"2호선"})

# 혼잡도 데이터 출처. 공식 파일(OA-12928)이 승하차 기반 추정치보다 항상 우선한다.
SOURCE_OFFICIAL = "official"
SOURCE_ESTIMATED = "estimated"
SOURCE_NONE = "none"
SOURCE_PRIORITY = (SOURCE_OFFICIAL, SOURCE_ESTIMATED)


def best_source(sources: Iterable[str]) -> str:
    """섞여 있는 출처들 중 가장 신뢰할 수 있는 하나를 고른다.

    화면에 '이 숫자가 공식 통계인지 추정치인지'를 한 줄로 표시해야 하는데,
    여러 역·시간대를 묶어 보여줄 때는 출처가 섞인다. 그럴 땐 가장 약한 쪽이 아니라
    실제로 쓰인 쪽을 우선순위대로 고른다.
    """
    present = set(sources)
    for source in SOURCE_PRIORITY:
        if source in present:
            return source
    return SOURCE_NONE


def _env(name: str, default: str | None = None) -> str | None:
    """환경변수를 읽되 빈 문자열은 '없음'으로 본다.

    도커 컴포즈는 값이 없는 변수를 빈 문자열로 넘긴다(`${VAR:-}`).
    이걸 그대로 두면 인증키가 '' 로 설정된 것처럼 보여, 키가 없다는 사실을
    감지하지 못하고 엉뚱한 401 을 받게 된다.
    """
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    realtime_api_key: str | None
    db_path: Path
    raw_dir: Path
    snapshot_dir: Path
    # 실시간 인증키 기본 한도가 일 1,000회라 온디맨드 호출 + TTL 캐시로 억제한다.
    realtime_cache_ttl_sec: int
    # 이번/다음 열차 혼잡도 차이가 이 값(%p) 미만이면 추천하지 않고 "비슷함" 처리.
    similar_threshold_pct: float

    @property
    def realtime_enabled(self) -> bool:
        return bool(self.realtime_api_key)


def _int_env(name: str, default: int) -> int:
    """숫자 설정. 오타로 앱이 죽지 않게 기본값으로 되돌린다."""
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r 을 정수로 읽을 수 없어 기본값 %d 을 씁니다.", name, raw, default)
        return default


def _float_env(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r 을 실수로 읽을 수 없어 기본값 %s 를 씁니다.", name, raw, default)
        return default


def load_settings() -> Settings:
    """`.env` 를 환경변수로 올린 뒤 설정을 읽는다.

    override=False 라서 이미 셸이나 도커가 넣어 준 값은 그대로 둔다.
    """
    load_dotenv(ENV_FILE, override=False)

    return Settings(
        api_key=_env("SEOUL_API_KEY"),
        realtime_api_key=_env("SEOUL_REALTIME_API_KEY"),
        db_path=Path(_env("SUBWAY_DB_PATH") or (DATA_DIR / "subway.duckdb")),
        raw_dir=Path(_env("SUBWAY_RAW_DIR") or RAW_DIR),
        snapshot_dir=Path(_env("SUBWAY_SNAPSHOT_DIR") or SNAPSHOT_DIR),
        realtime_cache_ttl_sec=_int_env("REALTIME_CACHE_TTL", 30),
        similar_threshold_pct=_float_env("SIMILAR_THRESHOLD_PCT", 8.0),
    )
