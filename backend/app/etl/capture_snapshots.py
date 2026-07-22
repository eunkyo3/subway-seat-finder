"""실시간 스냅샷 녹화.

    python -m backend.app.etl.capture_snapshots --lines 2호선 4호선 --rounds 20 --interval 30

발표 중 네트워크·API 장애에 대비한 안전망이다. 실제 API 응답을 그대로 저장해 두면,
장애 시 앱이 그 스냅샷을 재생하며 시연을 이어간다. **합성 데이터가 아니라 실측 기록**이라
재생 화면도 진짜 있었던 열차 배치를 보여준다.

동시에 이 스크립트가 유일한 로그 수집기다. 앱은 DB 를 읽기 전용으로만 열기 때문에
train_position_log(시발 감지) / arrival_log(배차간격 캘리브레이션)는 여기서만 쌓인다.
앱이 DB 를 잡고 있으면 로그 적재는 불가능하므로, 로그가 목적이면 앱을 내리고 실행한다.

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
    parser.add_argument(
        "--require-db",
        action="store_true",
        help="DB 쓰기 연결을 못 잡으면 스냅샷만 남기지 않고 즉시 실패한다."
        " 위치 로그 축적이 목적일 때 조용한 실패를 막는다.",
    )
    parser.add_argument(
        "--no-arrivals",
        action="store_true",
        help="도착정보(전 역 일괄) 수집을 끈다. 기본은 라운드당 1회 수집해"
        " arrival_log 를 함께 쌓는다(배차간격 캘리브레이션용).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)
    settings = load_settings()

    if not settings.realtime_enabled:
        logger.error(
            "실시간 인증키가 없어 녹화할 수 없습니다.\n"
            "  https://data.seoul.go.kr/together/mypage/actkeyMain.do 에서\n"
            "  '실시간 지하철 인증키 신청'(일반 인증키와 별개)을 받아\n"
            "  .env 의 SEOUL_REALTIME_API_KEY 에 넣으세요."
        )
        return 1

    # 일 1,000회 한도가 있어 녹화량을 미리 알려준다.
    arrival_calls = 0 if args.no_arrivals else args.rounds
    total_calls = len(args.lines) * args.rounds + arrival_calls
    logger.info(
        "%d개 노선 × %d회 + 도착정보 %d회 = 총 %d회 호출 예정 (일 한도 1,000회)",
        len(args.lines), args.rounds, arrival_calls, total_calls,
    )
    if total_calls > 1000:
        logger.warning("한도를 넘습니다. --rounds 를 줄이거나 갤러리 등록으로 제약을 해제하세요.")

    settings.snapshot_dir.mkdir(parents=True, exist_ok=True)

    # 위치 로그 적재는 부가 기능이다. 앱이 떠 있으면 DuckDB 쓰기 연결을 못 잡는데,
    # 그렇다고 녹화 자체를 포기할 이유는 없다. 스냅샷은 파일로 남으면 그만이다.
    # 단, 이 실패를 조용히 넘기면 시발 보정이 죽은 채로 숨는다(§A). 반드시 결과를
    # 명시하고, 로그가 목적이면 --require-db 로 실패를 실패로 처리한다.
    con = None
    try:
        con = connect(settings.db_path)
    except duckdb.IOException:
        if args.require_db:
            logger.error(
                "DB 를 쓰기로 열 수 없습니다 (--require-db). "
                "앱이 파일을 잡고 있으면 먼저 내리세요: docker compose down"
            )
            return 1
        logger.warning(
            "DB 를 쓰기로 열 수 없어 위치 로그 적재는 건너뜁니다(스냅샷 저장은 계속). "
            "이 상태에서는 시발(始發) 보정이 계속 비활성화됩니다. "
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
            if not args.no_arrivals:
                # 전 역 일괄 조회 1회. 파일로는 남기지 않는다 — 앱의 replay 는 역 단위
                # 키로 스냅샷을 찾으므로 ALL 스냅샷은 재생되지 않는다. 목적은
                # arrival_log 축적(배차간격 캘리브레이션의 원천)이다.
                arrivals = client.fetch_all_arrivals()
                if arrivals.is_live:
                    logger.info(
                        "[%d/%d] 도착정보 %d건 수집",
                        round_index + 1, args.rounds, len(arrivals.records),
                    )
                else:
                    logger.warning(
                        "도착정보가 라이브 응답이 아니라 적재하지 않습니다(%s)",
                        arrivals.source,
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
