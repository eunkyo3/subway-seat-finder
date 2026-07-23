"""배차간격 캘리브레이션.

    python -m backend.app.etl.calibrate_headway [--day-type 평일] [--json 경로]

NOMINAL_HEADWAY_MIN(시간대별 기준 배차간격)은 공개 시각표에서 정성적으로 뽑은
값이고 노선 구분이 없다. 2호선과 8호선의 배차가 같을 리 없으므로, 수집된
arrival_log 에서 실측 간격 분포를 뽑아 **노선×시간대** 중앙값과 현재 상수의
편차를 보고한다. 표본이 충분한 셀만 제안으로 인정한다 — 몇 개 관측의 중앙값을
상수로 승격하면 캘리브레이션이 아니라 노이즈 이식이다.

HEADWAY_SENSITIVITY(간격→혼잡 민감도)는 이 데이터로 적합하지 않는다.
열차별 혼잡 실측이 없고, 시간대를 가로지르는 간격-혼잡 상관은 수요가
교란변수라 부호가 반대로 나온다(피크에는 배차도 짧고 혼잡도 높다).
억지로 맞춘 값보다 미보정 명시가 정직하다 — validate_estimate 와 같은 태도다.

이 스크립트는 보고만 한다. 상수를 바꾸는 판단은 사람이 표를 보고 내린다.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import timedelta
from pathlib import Path
from statistics import median

from ..config import load_settings
from ..db import connect
from ..naming import line_from_subway_id
from ..predict.signals import DEFAULT_NOMINAL_HEADWAY_MIN, NOMINAL_HEADWAY_MIN

logger = logging.getLogger("calibrate")

# 요일 유형은 혼잡도 통계(congestion_stat.day_type)와 같은 어휘를 쓴다.
DAY_TYPE_BY_WEEKDAY = {0: "평일", 1: "평일", 2: "평일", 3: "평일", 4: "평일", 5: "토요일", 6: "일요일"}

# 이보다 긴 간격은 배차가 아니라 운행 공백(막차 전후, 수집 공백)일 가능성이 높다.
MAX_HEADWAY_SEC = 3600.0

# 셀당 최소 표본. 이 밑이면 중앙값이 우연에 좌우된다.
MIN_SAMPLES = 30


def extract_headways(con, day_type: str) -> list[tuple[str, int, float]]:
    """arrival_log 에서 (노선, 시간대, 실측 간격초) 표본을 뽑는다.

    같은 수집 시각·역·방향 안에서 ETA 순으로 이웃한 두 열차의 차이가 간격이다.
    그룹 경계를 넘으면 다른 역·다른 방향·다른 시점의 열차를 이웃으로 붙여
    가짜 간격이 생기므로, 그룹이 바뀌면 이전 열차를 버린다.

    시간대는 수집 시각이 아니라 **도착 예정 시각**(collected_at + eta)으로
    버킷한다. 07:58 에 수집해도 08:03 도착이면 8시대 배차다.

    표본 유효성 두 가지를 지킨다.
    - eta=0 은 arrival_code 가 진입('0')/도착('1')일 때만 진짜 0초다. 컬럼 도입
      전 행(NULL)의 0 은 '카운트다운 미상'일 가능성이 높아 제외한다 — 미상 0 과
      다음 열차의 차이는 배차간격이 아니라 노이즈다.
    - 30초 폴링마다 같은 열차쌍의 간격이 반복 관측된다. 그대로 다 세면 표본 n 이
      부풀어 최소 표본 판정이 실제보다 후해지므로, 물리적으로 같은 간격
      (같은 역·방향·열차쌍·시간대)은 한 번만 센다.
    """
    rows = con.execute(
        "SELECT collected_at, subway_id, station_name, direction, train_no, arrival_eta_sec"
        " FROM arrival_log"
        " WHERE train_no <> '' AND collected_at IS NOT NULL"
        "   AND arrival_eta_sec IS NOT NULL"
        "   AND (arrival_eta_sec > 0 OR arrival_code IN ('0', '1'))"
        " ORDER BY collected_at, subway_id, station_name, direction, arrival_eta_sec"
    ).fetchall()

    samples: list[tuple[str, int, float]] = []
    seen: set[tuple] = set()
    prev_key = None
    prev_eta = None
    prev_train = None
    for collected_at, subway_id, station, direction, train_no, eta_sec in rows:
        if DAY_TYPE_BY_WEEKDAY[collected_at.weekday()] != day_type:
            continue
        line = line_from_subway_id(subway_id)
        if not line:
            continue

        key = (collected_at, line, station, direction)
        if key == prev_key and train_no != prev_train:
            gap = float(eta_sec - prev_eta)
            if 0 < gap <= MAX_HEADWAY_SEC:
                arrival_at = collected_at + timedelta(seconds=float(eta_sec))
                dedup_key = (
                    line, station, direction, prev_train, train_no,
                    arrival_at.date(), arrival_at.hour,
                )
                if dedup_key not in seen:
                    seen.add(dedup_key)
                    samples.append((line, arrival_at.hour, gap))
        prev_key, prev_eta, prev_train = key, eta_sec, train_no
    return samples


def summarize(
    samples: list[tuple[str, int, float]], *, min_samples: int = MIN_SAMPLES
) -> list[dict]:
    """노선×시간대 셀로 묶어 중앙값과 현재 상수 대비 편차를 계산한다."""
    cells: dict[tuple[str, int], list[float]] = {}
    for line, hour, gap in samples:
        cells.setdefault((line, hour), []).append(gap)

    out = []
    for (line, hour), gaps in sorted(cells.items()):
        observed_min = median(gaps) / 60.0
        nominal_min = NOMINAL_HEADWAY_MIN.get(hour, DEFAULT_NOMINAL_HEADWAY_MIN)
        out.append(
            {
                "line": line,
                "hour": hour,
                "n": len(gaps),
                "observedMedianMin": round(observed_min, 2),
                "nominalMin": nominal_min,
                "deviationPct": round((observed_min / nominal_min - 1.0) * 100, 1),
                "sufficient": len(gaps) >= min_samples,
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="arrival_log 실측 배차간격 vs 기준 상수 대조")
    parser.add_argument("--day-type", default="평일", choices=["평일", "토요일", "일요일"])
    parser.add_argument("--min-samples", type=int, default=MIN_SAMPLES)
    parser.add_argument("--json", type=Path, default=None, help="셀 요약을 JSON 으로 저장할 경로")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    settings = load_settings()
    con = connect(settings.db_path, read_only=True)
    try:
        samples = extract_headways(con, args.day_type)
    finally:
        con.close()

    if not samples:
        logger.error(
            "실측 간격 표본이 없습니다. 앱을 내린 뒤 capture_snapshots 로"
            " arrival_log 를 먼저 쌓으세요 (요일=%s).", args.day_type
        )
        return 1

    cells = summarize(samples, min_samples=args.min_samples)
    sufficient = [c for c in cells if c["sufficient"]]

    logger.info("실측 간격 표본: %d개, 노선×시간대 셀: %d개 (요일=%s)", len(samples), len(cells), args.day_type)
    # 출력 문자열에 em-dash(U+2014)를 쓰지 않는다. PYTHONUTF8 없는 Windows 콘솔(cp949)은
    # 이 문자를 인코딩하지 못해 로깅 핸들러가 트레이스백을 뱉는다. 도커는 PYTHONUTF8=1 이라 무관.
    logger.info("셀당 최소 표본 %d개. 미달 셀은 '표본부족'으로 표시하고 제안에서 제외한다.", args.min_samples)
    logger.info("")
    logger.info("[노선×시간대 실측 중앙값 vs NOMINAL_HEADWAY_MIN]")
    logger.info("  노선    시간   n     실측(분)  기준(분)  편차")
    for c in cells:
        logger.info(
            "  %-5s %3d시 %5d %8.2f %8.1f %+7.1f%% %s",
            c["line"], c["hour"], c["n"], c["observedMedianMin"], c["nominalMin"],
            c["deviationPct"], "" if c["sufficient"] else " (표본부족)",
        )

    logger.info("")
    logger.info("[해석]")
    if not sufficient:
        logger.info(
            "  모든 셀이 표본 부족이다. 아직 상수를 바꿀 근거가 없다. 여러 날·"
            "피크/비피크에 걸쳐 수집을 반복한 뒤 다시 실행하라."
        )
    else:
        worst = max(sufficient, key=lambda c: abs(c["deviationPct"]))
        logger.info(
            "  표본 충분 셀 %d개. 최대 편차는 %s %d시 %+.1f%%. 노선 구분 없는 현재"
            " 상수의 한계가 수치로 드러난 곳부터 노선별 값으로 나누는 것이 첫 개선이다.",
            len(sufficient), worst["line"], worst["hour"], worst["deviationPct"],
        )
    logger.info(
        "  HEADWAY_SENSITIVITY 는 이 데이터로 적합하지 않았다. 열차별 혼잡 실측이"
        " 없고,\n  시간대 간 간격-혼잡 상관은 수요가 교란변수라 부호가 반대로 나온다"
        "(피크에는\n  배차도 짧고 혼잡도 높다). 근거 없는 값보다 미보정 명시가 정직하다."
    )

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {"dayType": args.day_type, "sampleCount": len(samples),
                 "minSamples": args.min_samples, "cells": cells},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        logger.info("")
        logger.info("요약을 %s 에 저장했다.", args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
