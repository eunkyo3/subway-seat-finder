"""추정 혼잡도를 공식 통계와 대조해 오차를 측정한다.

    python -m backend.app.etl.validate_estimate

이 프로젝트의 가장 큰 약점은 실측 정답이 없다는 것이다(열차 내 재차인원 센서 없음).
그래서 공식 30분 평균 혼잡도(OA-12928)를 **의사정답(pseudo-ground-truth)** 으로 삼아
승하차 기반 추정치가 얼마나 벗어나는지 수치로 남긴다.

두 가지를 따로 본다.
- **수준(level)**: 절대 오차. 추정 스케일이 맞는가.
- **형상(shape)**: 순위 상관. "어디가 더 붐비는가"를 맞히는가.
  절대값이 틀려도 형상이 맞으면 '어느 열차를 탈지' 추천은 여전히 유효하다.
"""

from __future__ import annotations

import argparse
import logging
import sys

from ..config import SOURCE_ESTIMATED, SOURCE_OFFICIAL, load_settings
from ..db import connect

logger = logging.getLogger("validate")


def _spearman(pairs: list[tuple[float, float]]) -> float | None:
    """순위 상관계수. scipy 없이 계산한다(동점은 평균 순위)."""
    n = len(pairs)
    if n < 3:
        return None

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: values[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            average = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = average
            i = j + 1
        return out

    rx = ranks([p[0] for p in pairs])
    ry = ranks([p[1] for p in pairs])
    mean_x = sum(rx) / n
    mean_y = sum(ry) / n
    cov = sum((a - mean_x) * (b - mean_y) for a, b in zip(rx, ry))
    var_x = sum((a - mean_x) ** 2 for a in rx) ** 0.5
    var_y = sum((b - mean_y) ** 2 for b in ry) ** 0.5
    if var_x == 0 or var_y == 0:
        return None
    return cov / (var_x * var_y)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="추정 혼잡도 vs 공식 통계 오차 측정")
    parser.add_argument("--day-type", default="평일")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    settings = load_settings()
    con = connect(settings.db_path, read_only=True)
    try:
        # 추정치는 방향 구분이 약하므로 역·시간대 단위로 평균 내어 맞붙인다.
        rows = con.execute(
            """
            WITH o AS (
              SELECT line, name_norm, time_slot, avg(congestion_pct) AS pct
              FROM congestion_stat WHERE source = ? AND day_type = ?
              GROUP BY line, name_norm, time_slot
            ), e AS (
              SELECT line, name_norm, time_slot, avg(congestion_pct) AS pct
              FROM congestion_stat WHERE source = ? AND day_type = ?
              GROUP BY line, name_norm, time_slot
            )
            SELECT o.line, o.name_norm, o.time_slot, o.pct, e.pct
            FROM o JOIN e USING (line, name_norm, time_slot)
            """,
            [SOURCE_OFFICIAL, args.day_type, SOURCE_ESTIMATED, args.day_type],
        ).fetchall()

        if not rows:
            logger.error(
                "대조할 데이터가 없습니다. 공식 파일(data/raw/)과 추정치가 모두 적재됐는지 확인하세요."
            )
            return 1

        errors = [abs(o - e) for *_, o, e in rows]
        mae = sum(errors) / len(errors)
        rmse = (sum(x * x for x in errors) / len(errors)) ** 0.5
        bias = sum(e - o for *_, o, e in rows) / len(rows)
        rho = _spearman([(o, e) for *_, o, e in rows])

        logger.info("대조 표본: %d개 (요일=%s)", len(rows), args.day_type)
        logger.info("")
        logger.info("[수준] 절대 오차")
        logger.info("  MAE  %.1f %%p   (평균적으로 이만큼 빗나간다)", mae)
        logger.info("  RMSE %.1f %%p   (큰 오차에 가중)", rmse)
        logger.info("  편향 %+.1f %%p  (양수면 추정이 과대평가)", bias)
        logger.info("")
        logger.info("[형상] 순위 상관")
        if rho is None:
            logger.info("  계산 불가")
        else:
            logger.info("  Spearman ρ = %.3f  (1.0 이면 붐비는 순서를 완벽히 맞힘)", rho)

        logger.info("")
        logger.info("[노선별 MAE]")
        by_line: dict[str, list[float]] = {}
        for line, _name, _slot, official, estimated in rows:
            by_line.setdefault(line, []).append(abs(official - estimated))
        for line in sorted(by_line):
            values = by_line[line]
            logger.info("  %-5s %5.1f %%p  (n=%d)", line, sum(values) / len(values), len(values))

        logger.info("")
        logger.info("[해석]")
        # 해석문을 고정해 두면 수치가 나빠져도 좋게 읽히므로, 실제 값에서 뽑는다.
        if rho is None:
            shape = "형상 신뢰도를 판단할 표본이 부족하다"
        elif rho >= 0.8:
            shape = f"붐비는 순서를 잘 맞힌다 (ρ={rho:.2f})"
        elif rho >= 0.5:
            shape = f"붐비는 순서를 어느 정도 맞힌다 (ρ={rho:.2f})"
        elif rho >= 0.3:
            shape = f"붐비는 순서를 부분적으로만 맞힌다 (ρ={rho:.2f}, 약한 상관)"
        else:
            shape = f"붐비는 순서를 거의 맞히지 못한다 (ρ={rho:.2f})"

        level = (
            f"평균 {mae:.0f}%p 빗나가며, "
            + ("전반적으로 과소평가한다" if bias < -3 else
               "전반적으로 과대평가한다" if bias > 3 else "체계적 편향은 작다")
        )
        logger.info("  수준: %s.", level)
        logger.info("  형상: %s.", shape)
        logger.info(
            "  → 추정치는 공식 통계의 대체재가 아니다. 1~8호선은 official 이 우선 적용되므로\n"
            "     이 오차가 예측에 들어가지 않는다. 이 수치의 용도는 '공식 통계가 없는 구간에서\n"
            "     추정치를 어디까지 믿을 수 있는가'의 상한을 정직하게 밝히는 것이다."
        )
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
