# 수집 서버 가이드 (Rocky Linux 9)

피크(출퇴근) 시간대의 실시간 배차 로그를 별도 서버에서 여러 날 수집해,
로컬로 가져와 `calibrate_headway` 표본을 확장하기 위한 절차입니다.

역할 분담이 명확합니다 — **서버는 수집만, 로컬은 병합·검증만** 합니다.
서버에서는 앱(uvicorn/docker)을 띄우지 마세요. DuckDB 쓰기 연결이 하나뿐이라
앱이 DB 를 잡으면 로그 적재가 실패합니다 (`--require-db` 가 이를 실패로 처리합니다).

---

## 1. 서버 준비 (최초 1회)

```bash
# 코드가 Python 3.10+ 문법을 씁니다. Rocky 9 기본 python3(3.9)로는 안 됩니다.
sudo dnf install -y python3.11 python3.11-pip git

git clone <저장소 URL> subway-seat-finder
cd subway-seat-finder

# .env 는 gitignore 라 clone 에 안 따라옵니다. 수집에는 실시간 키 하나만 필수입니다.
cp .env.example .env
vi .env    # SEOUL_REALTIME_API_KEY 만 채우면 됩니다 (SEOUL_API_KEY 는 ETL 용이라 불필요)

# 크론 시각이 KST 피크 기준입니다.
sudo timedatectl set-timezone Asia/Seoul
```

가상환경 생성·의존성 설치는 수집 스크립트가 첫 실행에서 자동으로 합니다.

## 2. 스모크 테스트 (본 수집 전 필수)

```bash
tools/server/collect_peak.sh smoke
```

1라운드(기본 2콜)로 API 라이브 응답과 DB 적재를 검증합니다. 성공하면 끝에
`train_position_log` / `arrival_log` 행 수가 출력됩니다. **여기서 실패를 잡는 것이
피크 시간을 통째로 날리는 것보다 쌉니다.**

## 3. 본 수집

### 크론 등록 (권장)

```bash
sudo dnf install -y cronie && sudo systemctl enable --now crond   # 없을 때만
tools/server/install_cron.sh              # 평일 07:00 / 18:00 KST, 멱등(재실행 안전)
tools/server/install_cron.sh uninstall    # 수집 종료 시
```

### 수동 실행

```bash
tools/server/collect_peak.sh peak
```

> 크론 창(07:00–08:50 / 18:00–19:50)과 겹치게 수동 실행하면 DuckDB 쓰기 연결
> 경합으로 한쪽이 실패합니다 (`--require-db` 라 조용히 죽지는 않습니다).

### 파라미터 (환경변수)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `COLLECT_LINES` | `2호선` | 수집 노선, 공백 구분 (`"2호선 5호선"`) |
| `COLLECT_INTERVAL` | `60` | 라운드 간격(초). `REALTIME_CACHE_TTL`(기본 30초) 미만이면 캐시 재사용으로 아무것도 안 쌓여서 스크립트가 거부합니다 |
| `COLLECT_ROUNDS` | `110` | 라운드 수 (110×60초 ≈ 110분 = 피크 커버) |
| `COLLECT_FORCE` | — | `1` 이면 1회 450콜 초과 실행 허용 |

### API 예산 ⚠️

실시간 키는 **일일 1,000콜 한도**입니다. 1라운드 = 위치 조회(노선 수) + 도착 전역 조회(1).

```
기본값: 110라운드 × (1노선 + 1) = 220콜/회 → 아침·저녁 2회 = 440콜/일
```

노선을 늘리면 콜이 정비례로 늡니다. 스크립트가 실행 전 예상 콜을 출력하고,
1회 450콜을 넘으면 `COLLECT_FORCE=1` 없이는 중단합니다. 배차 캘리브레이션의
원천은 도착 로그(노선 수와 무관하게 1콜)이므로, **한도가 걱정되면 노선을 줄이는
것이 라운드를 줄이는 것보다 낫습니다.**

실행 기록은 `data/collect_<타임스탬프>.log` 에 남습니다.

## 4. 결과 회수

**수집이 돌고 있지 않을 때** 실행하세요 — 쓰기 중인 DuckDB 를 tar 로 뜨면
체크포인트 전 데이터가 빠진 사본이 나옵니다. 크론 창(07:00–08:50 / 18:00–19:50)을
피하거나, 수집을 끝낼 거면 먼저 `tools/server/install_cron.sh uninstall` 하세요.

```bash
cd subway-seat-finder

# 수집이 정상 종료됐다면 이 파일은 없어야 합니다. 있으면 수집이 진행 중이거나
# 비정상 종료 상태이니 tar 를 멈추고 원인을 확인하세요.
ls data/subway.duckdb.wal 2>/dev/null && echo "WAL 존재 — 회수 중단" || echo "OK"

tar czf collected_$(date +%Y%m%d).tar.gz data/subway.duckdb data/snapshots data/collect_*.log
```

로컬로 가져옵니다 (예: `scp 서버:~/subway-seat-finder/collected_*.tar.gz .`).

## 5. 로컬 병합

압축을 풀고 (로컬 앱은 내린 상태에서):

```bash
python -m backend.app.etl.merge_server_logs 서버압축푼곳/data/subway.duckdb \
    --snapshots 서버압축푼곳/data/snapshots
```

- **멱등**: 이미 병합한 행·스냅샷은 다시 넣지 않습니다. 같은 파일을 두 번 돌려도 안전합니다.
- `--dry-run` 으로 쓰기 전에 삽입될 행 수를 미리 볼 수 있습니다.
- 서버 스냅샷은 피크 시간대 실측이라 **시연용 재생 데이터를 겸합니다.**

## 6. 검증

```bash
python -m backend.app.etl.calibrate_headway    # 노선×시간대 실측 배차 vs 기준 상수
```

표본이 늘었으니 README 의 캘리브레이션 수치(현재 22시대 단일 창 기준)를
새 결과로 갱신합니다. `MIN_SAMPLES` 게이트를 넘는 셀이 생기면 그때 상수 반영을
판단합니다 — 스크립트는 보고만 하고, 반영은 사람이 결정합니다.
