"""설정 로딩.

인증키는 두 종류다.
- 일반 인증키: openapi.seoul.go.kr:8088 (역 마스터, 승하차 통계)
- 실시간 지하철 인증키: swopenapi.seoul.go.kr (열차 위치, 도착)
  별도 신청 대상이라 없을 수 있다. 없으면 실시간 계층이 replay 로 폴백한다.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
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


def _read_key_file(path: Path) -> str | None:
    """키 파일을 읽는다. 주석(#)과 빈 줄은 건너뛴다."""
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return None


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


def load_settings() -> Settings:
    """환경변수 > 키 파일 순으로 설정을 읽는다."""
    api_key = os.getenv("SEOUL_API_KEY") or _read_key_file(PROJECT_ROOT / "api-key.txt")
    realtime_key = os.getenv("SEOUL_REALTIME_API_KEY") or _read_key_file(
        PROJECT_ROOT / "realtime-api-key.txt"
    )

    db_path = Path(os.getenv("SUBWAY_DB_PATH") or (DATA_DIR / "subway.duckdb"))
    raw_dir = Path(os.getenv("SUBWAY_RAW_DIR") or RAW_DIR)
    snapshot_dir = Path(os.getenv("SUBWAY_SNAPSHOT_DIR") or SNAPSHOT_DIR)

    return Settings(
        api_key=api_key,
        realtime_api_key=realtime_key,
        db_path=db_path,
        raw_dir=raw_dir,
        snapshot_dir=snapshot_dir,
        realtime_cache_ttl_sec=int(os.getenv("REALTIME_CACHE_TTL", "30")),
        similar_threshold_pct=float(os.getenv("SIMILAR_THRESHOLD_PCT", "8")),
    )
