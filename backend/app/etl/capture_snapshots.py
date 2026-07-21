"""실시간 스냅샷 녹화.

    python -m backend.app.etl.capture_snapshots --lines 2호선 4호선 --rounds 20 --interval 30

발표 중 네트워크·API 장애에 대비한 안전망이다. 실제 API 응답을 그대로 저장해 두면,
장애 시 앱이 그 스냅샷을 재생하며 시연을 이어간다. **합성 데이터가 아니라 실측 기록**이라
재생 화면도 진짜 있었던 열차 배치를 보여준다.

실시간 인증키가 없으면 아무것도 녹화하지 않고 안내만 남긴다.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import duckdb

from ..clients.realtime import RealtimeClient
from ..config import PREDICTABLE_LINES, load_settings
from ..db import connect

logger = logging.getLogger("capture")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="실시간 위치 스냅샷 녹화")
    parser.add_argument("--lines", nargs="*", default=list(PREDICTABLE_LINES))
    parser.add_argument("--rounds", type=int, default=10, help="반복 횟수")
    parser.add_argument("--interval", type=float, default=30.0, help="반복 간격(초)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)
    settings = load_settings()

    if not settings.realtime_enabled:
        logger.error(
            "실시간 인증키가 없어 녹화할 수 없습니다.\n"
            "  https://data.seoul.go.kr/together/mypage/actkeyMain.do 에서\n"
            "  '실시간 지하철 인증키 신청'(일반 인증키와 별개)을 받아\n"
            "  realtime-api-key.txt 에 저장하세요."
        )
        return 1

    # 일 1,000회 한도가 있어 녹화량을 미리 알려준다.
    total_calls = len(args.lines) * args.rounds
    logger.info(
        "%d개 노선 × %d회 = 총 %d회 호출 예정 (일 한도 1,000회)",
        len(args.lines), args.rounds, total_calls,
    )
    if total_calls > 1000:
        logger.warning("한도를 넘습니다. --rounds 를 줄이거나 갤러리 등록으로 제약을 해제하세요.")

    settings.snapshot_dir.mkdir(parents=True, exist_ok=True)

    # 위치 로그 적재는 부가 기능이다. 앱이 떠 있으면 DuckDB 쓰기 연결을 못 잡는데,
    # 그렇다고 녹화 자체를 포기할 이유는 없다. 스냅샷은 파일로 남으면 그만이다.
    con = None
    try:
        con = connect(settings.db_path)
    except duckdb.IOException:
        logger.warning(
            "DB 를 쓰기로 열 수 없어 위치 로그 적재는 건너뜁니다(스냅샷 저장은 계속). "
            "로그까지 쌓으려면 앱을 멈춘 뒤 실행하세요."
        )

    saved = 0
    try:
        client = RealtimeClient(settings, con=con)
        for round_index in range(args.rounds):
            for line in args.lines:
                result = client.fetch_positions(line)
                if not result.is_live:
                    logger.warning("%s: 라이브 응답이 아니라 건너뜁니다(%s)", line, result.source)
                    continue
                path = client.save_snapshot(result)
                if path:
                    saved += 1
                    logger.info(
                        "[%d/%d] %s %d대 -> %s",
                        round_index + 1, args.rounds, line, len(result.records), path.name,
                    )
            if round_index < args.rounds - 1:
                time.sleep(args.interval)
        client.close()
    finally:
        if con is not None:
            con.close()

    logger.info("스냅샷 %d개를 %s 에 저장했습니다.", saved, settings.snapshot_dir)
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
