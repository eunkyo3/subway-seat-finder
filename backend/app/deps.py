"""요청 처리에 필요한 공용 자원.

DuckDB 는 한 프로세스에서 쓰기 연결이 하나뿐이라, 앱은 읽기 전용으로 한 번만 열고
요청마다 커서를 뜬다. 실시간 클라이언트는 TTL 캐시를 공유해야 하므로 프로세스에
하나만 둔다 — 요청마다 새로 만들면 캐시가 매번 비어 일 1,000회 한도를 넘긴다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import duckdb

from .clients.realtime import RealtimeClient
from .config import Settings, load_settings
from .db import connect, init_schema

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    settings: Settings
    con: duckdb.DuckDBPyConnection
    realtime: RealtimeClient

    def cursor(self) -> duckdb.DuckDBPyConnection:
        return self.con.cursor()

    def close(self) -> None:
        self.realtime.close()
        self.con.close()


def _warn_if_origin_log_empty(con: duckdb.DuckDBPyConnection) -> None:
    """위치 로그가 비어 있으면 기동 시점에 알린다.

    앱은 읽기 전용이라 로그를 스스로 쌓지 못한다. 로그가 비면 시발 보정이
    코드로는 존재해도 한 번도 발동하지 않는데(§A), 이 사실이 어디에도 드러나지
    않아 오래 숨었다. 침묵 대신 기동 로그 한 줄로 상태를 못박는다.
    """
    try:
        count = con.execute("SELECT count(*) FROM train_position_log").fetchone()[0]
    except duckdb.CatalogException:
        # 쓰기 연결이 스키마를 만들기 전의 오래된 DB. 없는 것도 0행과 같다.
        count = 0
    if count == 0:
        logger.warning(
            "train_position_log 가 비어 있어 시발(始發) 보정이 항상 비활성화됩니다. "
            "앱을 내린 뒤 python -m backend.app.etl.capture_snapshots 를 실행해 "
            "열차 위치 로그를 쌓으세요."
        )


def build_state(settings: Settings | None = None) -> AppState:
    settings = settings or load_settings()

    if settings.db_path.exists():
        con = connect(settings.db_path, read_only=True)
        _warn_if_origin_log_empty(con)
    else:
        # ETL 전에도 앱은 떠야 한다. 빈 스키마로 열고 화면이 '데이터 없음'을 보이게 한다.
        logger.warning(
            "%s 가 없습니다. ETL(python -m backend.app.etl.run_all)을 먼저 실행하세요.",
            settings.db_path,
        )
        con = duckdb.connect(":memory:")
        init_schema(con)

    if not settings.realtime_enabled:
        logger.warning(
            "실시간 인증키가 없어 재생(replay) 모드로 동작합니다. "
            "https://data.seoul.go.kr/together/mypage/actkeyMain.do 에서 "
            "'실시간 지하철 인증키'를 신청해 .env 의 SEOUL_REALTIME_API_KEY 에 넣으세요."
        )

    return AppState(settings=settings, con=con, realtime=RealtimeClient(settings))
