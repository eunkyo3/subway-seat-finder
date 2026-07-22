# 피크(08:00~08:30) 스냅샷 재녹화 — 인수인계 §C.
#
# 기존 스냅샷 16개(2026-07-21 15:16, 비피크)만으로는 발표 중 replay 폴백 시
# 화면이 온통 "여유"로 나온다. 아침 피크에 실제 API 응답을 다시 녹화한다.
# 앱(도커)이 떠 있으면 먼저 내린다 — DuckDB 쓰기 잠금이 풀려야 위치/도착
# 로그도 함께 쌓인다(§A). 끝나면 떠 있던 경우에만 다시 올린다.
#
# 수동 실행:  powershell -NoProfile -ExecutionPolicy Bypass -File tools\record_peak_snapshots.ps1
# 호출량:     8개 노선 × 20회 + 도착 20회 = 180회 (일 한도 1,000회)

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$log = Join-Path $repo "data\peak_capture_$stamp.log"
"[$(Get-Date)] peak capture start" | Out-File $log -Encoding utf8

$dockerWasUp = $false
$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($docker) {
    $running = docker compose ps --quiet 2>$null
    if ($LASTEXITCODE -eq 0 -and $running) {
        $dockerWasUp = $true
        "[$(Get-Date)] docker compose down" | Out-File $log -Append -Encoding utf8
        cmd /c "docker compose down >> `"$log`" 2>&1"
    }
}

cmd /c "`"$repo\.venv\Scripts\python.exe`" -m backend.app.etl.capture_snapshots --rounds 20 --interval 30 >> `"$log`" 2>&1"
$rc = $LASTEXITCODE
"[$(Get-Date)] capture exit code: $rc" | Out-File $log -Append -Encoding utf8

if ($dockerWasUp) {
    "[$(Get-Date)] docker compose up -d" | Out-File $log -Append -Encoding utf8
    cmd /c "docker compose up -d >> `"$log`" 2>&1"
}

exit $rc
