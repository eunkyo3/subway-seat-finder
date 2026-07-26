#!/usr/bin/env bash
# 피크 실시간 로그 수집 (Rocky Linux 9 수집 서버용) — docs/SERVER_COLLECTION.md
#
#   tools/server/collect_peak.sh smoke   # 1라운드로 API·DB 적재 검증 (본 수집 전 필수)
#   tools/server/collect_peak.sh peak    # 본 수집 (기본: 2호선, 60초 간격, 110라운드 ≈ 110분)
#
# 조정은 환경변수로:
#   COLLECT_LINES="2호선 5호선"  수집 노선 (공백 구분)
#   COLLECT_INTERVAL=60          라운드 간격(초)
#   COLLECT_ROUNDS=110           라운드 수
#   COLLECT_FORCE=1              1회 450콜 초과 실행을 허용
#
# API 예산: 1라운드 = 위치(노선 수) + 도착 ALL(1) 콜. 실시간 키 일일 한도 1,000회.
# 기본값은 110×(1+1)=220콜/회 — 아침·저녁 2회 돌려도 440콜로 한도의 절반 이하다.
set -euo pipefail

MODE="${1:-peak}"
case "$MODE" in
  peak|smoke) ;;
  *) echo "사용법: $0 [peak|smoke]" >&2; exit 2 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# --- 파이썬 탐지 -------------------------------------------------------------
# 코드가 3.10+ 문법(X | None)을 쓴다. Rocky 9 기본 python3 는 3.9 라 안 된다.
find_python() {
  local cand
  for cand in python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1 \
       && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      echo "$cand"
      return 0
    fi
  done
  return 1
}

PY="$(find_python || true)"
if [ -z "$PY" ]; then
  echo "Python 3.10+ 가 없습니다. Rocky Linux 9 에서는:" >&2
  echo "  sudo dnf install -y python3.11 python3.11-pip" >&2
  exit 1
fi

# --- 가상환경 ---------------------------------------------------------------
# 준비 완료 판정은 스탬프 파일로 한다. venv 생성 후 pip install 이 실패하면
# (크론 첫 실행 중 네트워크 순단 등) python 은 있는데 의존성이 없는 반쪽 상태가
# 남는데, python 존재만 보면 다음 실행이 설치를 건너뛰고 영영 못 벗어난다.
VENV="$ROOT/.venv"
STAMP="$VENV/.deps-ok"
if [ ! -f "$STAMP" ]; then
  echo "[준비] 가상환경·의존성 설치: $VENV ($("$PY" --version))"
  "$PY" -m venv "$VENV"   # 이미 있으면 재사용된다 — 반쪽 venv 도 이어서 완성
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet -r requirements.txt
  touch "$STAMP"
fi

# --- 설정 확인 --------------------------------------------------------------
# 실시간 키가 없으면 수집기가 replay 폴백으로 내려가 라이브 로그가 안 쌓인다.
# 여기서 미리 막는 편이 피크 시간을 통째로 날리는 것보다 낫다.
if [ -z "${SEOUL_REALTIME_API_KEY:-}" ]; then
  if [ ! -f .env ]; then
    echo ".env 가 없습니다: cp .env.example .env 후 SEOUL_REALTIME_API_KEY 를 채우세요." >&2
    exit 1
  fi
  if ! grep -qE '^SEOUL_REALTIME_API_KEY=.+' .env; then
    echo ".env 의 SEOUL_REALTIME_API_KEY 가 비어 있습니다. 실시간 키 없이는 라이브 수집이 안 됩니다." >&2
    exit 1
  fi
fi

# --- 수집 파라미터와 API 예산 -----------------------------------------------
LINES="${COLLECT_LINES:-2호선}"
INTERVAL="${COLLECT_INTERVAL:-60}"
ROUNDS="${COLLECT_ROUNDS:-110}"
if [ "$MODE" = smoke ]; then
  ROUNDS=1
fi

# 간격이 캐시 TTL 보다 짧으면 실시간 클라이언트가 캐시를 재사용해 API 호출도
# 로그 적재도 일어나지 않는다 — 조용히 아무것도 안 쌓이는 최악의 실패다.
# grep 매치 실패/파일 부재가 errexit 로 번지지 않게 || true — 빈 값은 아래서 30 으로 폴백
TTL="${REALTIME_CACHE_TTL:-$(grep -E '^REALTIME_CACHE_TTL=' .env 2>/dev/null | tail -1 | cut -d= -f2 || true)}"
case "$TTL" in ''|*[!0-9]*) TTL=30 ;; esac
if [ "$INTERVAL" -lt "$TTL" ]; then
  echo "COLLECT_INTERVAL(${INTERVAL}초)이 캐시 TTL(${TTL}초)보다 짧습니다." >&2
  echo "캐시가 재사용돼 새 데이터가 쌓이지 않습니다. 간격을 TTL 이상으로 잡으세요." >&2
  exit 1
fi

NLINES=$(echo "$LINES" | wc -w)
CALLS=$(( ROUNDS * (NLINES + 1) ))
echo "[예산] 노선 ${NLINES}개 × ${ROUNDS}라운드 → 예상 API 호출 ${CALLS}콜 (일일 한도 1,000)"
if [ "$CALLS" -gt 450 ] && [ "${COLLECT_FORCE:-0}" != 1 ]; then
  echo "1회 실행이 450콜을 넘습니다. 하루 2회(아침·저녁) 돌리면 한도를 위협합니다." >&2
  echo "의도한 것이면 COLLECT_FORCE=1 로 다시 실행하세요." >&2
  exit 1
fi

# --- 실행 -------------------------------------------------------------------
mkdir -p data
LOG="data/collect_$(date +%Y%m%d-%H%M%S).log"
echo "[수집] $MODE — 노선: $LINES, 간격: ${INTERVAL}초, 라운드: $ROUNDS (로그: $LOG)"

# --require-db: 로그 적재 실패(다른 프로세스가 DB 점유)를 조용히 넘기지 않는다.
# 수집 서버에서는 앱을 띄우지 않으므로 이 실패는 곧 설정 오류다.
# set -e 를 잠시 풀어 실패해도 아래 안내까지 도달하게 한다.
set +e
# shellcheck disable=SC2086  # LINES 는 의도된 단어 분리(노선 목록 인자)
"$VENV/bin/python" -m backend.app.etl.capture_snapshots \
  --require-db --lines $LINES --rounds "$ROUNDS" --interval "$INTERVAL" 2>&1 | tee "$LOG"
STATUS=${PIPESTATUS[0]}
set -e

if [ "$STATUS" -eq 0 ]; then
  echo "[완료] 로그 적재 확인:"
  "$VENV/bin/python" - <<'EOF'
from backend.app.config import load_settings
from backend.app.db import connect

con = connect(load_settings().db_path, read_only=True)
for table in ("train_position_log", "arrival_log"):
    count, last = con.execute(
        f"SELECT count(*), max(collected_at) FROM {table}"
    ).fetchone()
    print(f"  {table}: {count}행 (최근 {last})")
con.close()
EOF
else
  echo "[실패] 종료 코드 $STATUS — 로그를 확인하세요: $LOG" >&2
fi
exit "$STATUS"
