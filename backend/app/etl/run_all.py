"""ETL 단일 진입점.

    python -m backend.app.etl.run_all              # 최신 월 자동 탐색
    python -m backend.app.etl.run_all --months 202606 202605
    python -m backend.app.etl.run_all --skip-stations

배치 데이터를 '갈아끼우는' 작업이 이 스크립트다. 실시간 위치는 여기서 다루지 않는다.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

import duckdb

from ..clients.seoul_open import SeoulOpenClient
from ..config import load_settings
from ..db import connect
from .load_congestion import load_congestion
from .load_flow import latest_available_month, load_flow
from .load_stations import load_stations

logger = logging.getLogger("etl")

# 승하차 통계는 공개까지 두어 달 걸린다. 오늘 기준 과거로 거슬러 찾는다.
MONTH_PROBE_DEPTH = 8


def recent_months(today: date, depth: int = MONTH_PROBE_DEPTH) -> list[str]:
    months = []
    year, month = today.year, today.month
    for _ in range(depth):
        months.append(f"{year:04d}{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return months


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="서울 지하철 배치 데이터 적재")
    parser.add_argument("--months", nargs="*", help="적재할 월(YYYYMM). 생략하면 최신 1개월")
    parser.add_argument("--skip-stations", action="store_true", help="역 마스터 적재 건너뛰기")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    settings = load_settings()
    if not settings.api_key:
        logger.error("일반 인증키가 없습니다. api-key.txt 또는 SEOUL_API_KEY 를 설정하세요.")
        return 1

    try:
        con = connect(settings.db_path)
    except duckdb.IOException as exc:
        # DuckDB 는 쓰기 연결을 하나만 허용한다. 앱이 DB 를 열고 있으면 여기서 막히는데,
        # 원본 오류가 'Permission denied' 라 그것만으로는 원인을 알 수 없다.
        # DuckDB 가 점유 프로세스를 알려주는 경우가 있어 원문도 같이 남긴다.
        logger.error(
            "%s 를 열 수 없습니다. 앱이 이 DB 를 사용 중이면 먼저 멈춰야 합니다.\n"
            "  docker compose down && docker compose run --rm etl && docker compose up -d\n"
            "  (로컬 실행이면 uvicorn 을 종료한 뒤 다시 시도하세요.)\n"
            "  원본 오류: %s",
            settings.db_path,
            str(exc).strip().replace("\n", " "),
        )
        return 1
    try:
        with SeoulOpenClient(settings.api_key) as client:
            if not args.skip_stations:
                load_stations(con, client)

            months = args.months
            if not months:
                latest = latest_available_month(client, recent_months(date.today()))
                if not latest:
                    logger.error("승하차 데이터가 있는 월을 찾지 못했습니다.")
                    return 1
                months = [latest]
                logger.info("최신 승하차 월로 %s 를 선택했습니다.", latest)

            for month in months:
                load_flow(con, client, month)

        counts = load_congestion(con, settings.raw_dir)

        station_count = con.execute("SELECT count(*) FROM station_master").fetchone()[0]
        flow_count = con.execute("SELECT count(*) FROM station_flow").fetchone()[0]
        logger.info(
            "완료 — station_master=%d, station_flow=%d, congestion(official=%d, estimated=%d)",
            station_count,
            flow_count,
            counts["official"],
            counts["estimated"],
        )
        if counts["official"] == 0:
            logger.warning(
                "공식 혼잡도(OA-12928) 파일이 없어 추정치로 동작합니다. "
                "https://data.seoul.go.kr/dataList/OA-12928/F/1/datasetView.do 에서 받아 "
                "%s 에 넣으세요.",
                settings.raw_dir,
            )
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
