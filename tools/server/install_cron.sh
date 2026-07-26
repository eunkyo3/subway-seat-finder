#!/usr/bin/env bash
# 피크 수집 크론 등록/해제 (Rocky Linux 9) — docs/SERVER_COLLECTION.md
#
#   tools/server/install_cron.sh            # 평일 07:00 / 18:00 KST 수집 등록 (멱등)
#   tools/server/install_cron.sh uninstall  # 이 스크립트가 등록한 항목만 제거
set -euo pipefail

ACTION="${1:-install}"
case "$ACTION" in
  install|uninstall) ;;
  *) echo "사용법: $0 [install|uninstall]" >&2; exit 2 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$ROOT/tools/server/collect_peak.sh"
# 마커 주석으로 우리 항목을 식별한다. 재실행하면 기존 항목을 먼저 걷어내므로
# 몇 번을 돌려도 중복 등록되지 않고, uninstall 은 우리 것만 제거한다.
MARKER="# subway-seat-finder:collect"

if ! command -v crontab >/dev/null 2>&1; then
  echo "crontab 이 없습니다. Rocky Linux 9 에서는:" >&2
  echo "  sudo dnf install -y cronie && sudo systemctl enable --now crond" >&2
  exit 1
fi

case "$ROOT" in
  *" "*)
    echo "저장소 경로에 공백이 있어 크론 항목이 깨집니다: $ROOT" >&2
    echo "공백 없는 경로에 clone 하세요." >&2
    exit 1
    ;;
esac

# 아래 시각은 KST 피크(출근 07-09시 / 퇴근 18-20시) 기준이다.
TZNOW="$(date +%Z)"
if [ "$TZNOW" != "KST" ]; then
  echo "[경고] 서버 시간대가 $TZNOW 입니다. 크론 시각은 KST 기준 피크라 어긋납니다." >&2
  echo "  sudo timedatectl set-timezone Asia/Seoul" >&2
fi

EXISTING="$(crontab -l 2>/dev/null | grep -vF "$MARKER" || true)"

if [ "$ACTION" = uninstall ]; then
  printf '%s\n' "$EXISTING" | crontab -
  echo "[해제] $MARKER 항목을 제거했습니다."
else
  {
    printf '%s\n' "$EXISTING"
    echo "0 7 * * 1-5 $SCRIPT peak $MARKER"
    echo "0 18 * * 1-5 $SCRIPT peak $MARKER"
  } | crontab -
  echo "[등록] 평일 07:00 / 18:00 에 collect_peak.sh peak 를 실행합니다."
fi

echo "--- 현재 crontab ---"
crontab -l
