"""서울 실시간 지하철 API 클라이언트 (열차 위치 / 도착 정보).

일반 OpenAPI 와는 호스트도 인증키도 다르다.
    http://swopenapi.seoul.go.kr/api/subway/{KEY}/json/{SERVICE}/{START}/{END}/{arg}

응답 규약 (실측 확인):
- 정상   : {"errorMessage": {"code": "INFO-000", ...}, "realtimePositionList": [...]}
- 실패   : {"status": 500, "code": "ERROR-338",
            "message": "해당 인증키로는 실시간 서비스를 사용할 수 없습니다.", "total": 0}
  (실패 봉투는 errorMessage 없이 평평하게 오는 경우가 관측됐다. 두 형태를 모두 받는다.)

이 모듈이 존재하는 이유는 세 가지다.
1) 실시간 인증키는 별도 신청 대상이라 없을 수 있다. 없다고 앱이 죽으면 안 되므로
   스냅샷 replay 로 자동 폴백한다. 호출자에게는 절대 예외를 던지지 않는다.
2) 실시간 키 기본 한도가 일 1,000회다. TTL 캐시로 같은 노선/역의 연속 조회를 흡수한다.
3) recptnDt(원천 생성시각)와 현재시각의 차이(age_sec)를 붙여야 예측 엔진이
   "몇 초 전 위치인지"를 알고 보간할 수 있다. 원시 행에는 이 정보가 없다.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from ..config import Settings
from ..naming import (
    line_from_subway_id,
    normalize_direction,
    normalize_line,
    normalize_station,
)

BASE_URL = "http://swopenapi.seoul.go.kr/api/subway"
DEFAULT_START = 0
DEFAULT_END = 100
# 도착정보 전체 조회. 역 단위로 쪼개 부르면 호출 한도가 금방 마르므로 데모는 이걸 쓴다.
ARRIVAL_ALL_KEY = "ALL"

SERVICE_BY_KIND = {"position": "realtimePosition", "arrival": "realtimeStationArrival"}
LIST_KEY_BY_KIND = {
    "position": "realtimePositionList",
    "arrival": "realtimeArrivalList",
}

# trainSttus. 숫자 코드만 오고 사람이 읽을 표기는 우리가 붙여야 한다.
POSITION_STATUS = {"0": "진입", "1": "도착", "2": "출발", "3": "전역출발"}
# directAt=1 이 급행. 소스에 따라 한글/Y 로도 오는 것이 관측돼 함께 받는다.
_EXPRESS_TRUE = {"1", "Y", "y", "급행"}

SOURCE_LIVE = "live"
SOURCE_REPLAY = "replay"

# 조건에 맞는 데이터가 없을 때의 응답 코드. 오류가 아니라 '지금은 없음' 이다.
NO_DATA_CODE = "INFO-200"


class RealtimeApiError(RuntimeError):
    """INFO-000 이 아닌 응답. 이 모듈 밖으로는 나가지 않고 replay 폴백으로 흡수된다."""

    def __init__(self, code: str, message: str, service: str) -> None:
        super().__init__(f"[{service}] {code}: {message}")
        self.code = code
        self.message = message
        self.service = service


@dataclass(frozen=True)
class RealtimeResult:
    """실시간 조회 1회의 결과.

    source 가 replay 면 과거 스냅샷이므로 신규 관측이 아니다. 저장하지 않는다.
    payload 는 save_snapshot() 으로 데모용 스냅샷을 남기기 위한 원시 응답이며
    replay 결과에는 없다(다시 저장해 스냅샷이 증식하는 것을 막는다).
    """

    source: str
    kind: str
    key: str
    fetched_at: datetime
    records: list[dict[str, Any]] = field(default_factory=list)
    payload: dict[str, Any] | None = None

    @property
    def is_live(self) -> bool:
        return self.source == SOURCE_LIVE


def _parse_reception_dt(raw: Any) -> datetime | None:
    """recptnDt 를 naive local datetime 으로 읽는다.

    API 는 KST 로컬 표기만 주고 오프셋이 없다. 서버도 KST 로 도는 전제라
    tz 를 붙이지 않고 naive 로 다뤄야 뺄셈이 어긋나지 않는다.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _age_sec(reception_dt: datetime | None, now: datetime) -> float | None:
    """원천 생성시각 대비 경과 초. 시각을 모르면 None."""
    if reception_dt is None:
        return None
    delta = (now - reception_dt).total_seconds()
    # 수집 서버와 원천의 시계 오차로 미래 시각이 오는 경우가 있다.
    # 음수 나이는 하류 보간에서 의미가 없으므로 0 으로 접는다.
    return delta if delta > 0 else 0.0


def _is_express(raw: Any) -> bool:
    return str(raw or "").strip() in _EXPRESS_TRUE


def _direction(raw: Any) -> str:
    """updnLine 을 '상선'/'하선' 으로 편다.

    혼잡도 통계와 같은 어휘를 써야 방향별 기준값을 찾을 수 있다. 실시간 원본은
    코드("0"/"1")로 오기도 하고 '상행'/'내선' 으로 오기도 한다.
    """
    return normalize_direction(raw)


def _to_int(raw: Any) -> int:
    text = str(raw or "").strip()
    try:
        return int(float(text))
    except ValueError:
        return 0


def _extract_envelope(payload: dict[str, Any]) -> tuple[str, str]:
    """(code, message) 를 뽑는다. 정상 응답은 errorMessage 안, 실패는 최상위에 있다."""
    envelope = payload.get("errorMessage")
    if isinstance(envelope, dict):
        return str(envelope.get("code") or ""), str(envelope.get("message") or "")
    return str(payload.get("code") or ""), str(payload.get("message") or "")


def _unwrap(payload: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    """응답 봉투를 벗겨 원시 행 목록을 돌려준다.

    INFO-200(데이터 없음)은 실패가 아니다. 막차가 끊긴 시간대에는 운행 중인 열차가
    정말로 없다. 이걸 장애로 처리해 스냅샷을 재생하면 새벽 3시에 유령 열차가 뜬다.
    """
    service = SERVICE_BY_KIND[kind]
    code, message = _extract_envelope(payload)
    if code == NO_DATA_CODE:
        return []
    if code and code != "INFO-000":
        raise RealtimeApiError(code, message or "실시간 API 오류", service)

    rows = payload.get(LIST_KEY_BY_KIND[kind])
    if not isinstance(rows, list):
        raise RealtimeApiError(
            code or "UNKNOWN", message or "응답에 목록이 없습니다.", service
        )
    return [r for r in rows if isinstance(r, dict)]


def _terminal_name(raw: Any) -> str:
    """종착역명을 정규화한다.

    위치 API 는 '성수종착' 처럼 꼬리를 붙여 준다. 도착 API 의 '신사' 와 같은 역인데
    문자열이 달라 조인이 어긋나므로 꼬리를 떼고 맞춘다.
    """
    text = str(raw or "").strip()
    for suffix in ("종착", "행"):
        if len(text) > len(suffix) and text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return normalize_station(text)


def _normalize_position(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    reception_dt = _parse_reception_dt(row.get("recptnDt"))
    station_raw = str(row.get("statnNm") or "")
    return {
        "kind": "position",
        "subway_id": str(row.get("subwayId") or ""),
        "line": line_from_subway_id(row.get("subwayId")),
        # 위치 응답의 열차번호는 trainNo 다. 도착 응답만 btrainNo 를 쓴다.
        # 여기를 틀리면 열차번호가 통째로 비어 시발 감지·배차간격이 조용히 죽는다.
        "train_no": str(row.get("trainNo") or ""),
        "station_id": str(row.get("statnId") or ""),
        "station_name": normalize_station(station_raw),
        "station_name_raw": station_raw,
        "direction": _direction(row.get("updnLine")),
        "express": _is_express(row.get("directAt")),
        # 위치 응답의 종착역은 statnTnm(예: '성수종착'). bstatnNm 은 없다.
        "terminal_station": _terminal_name(row.get("statnTnm")),
        "last_train": str(row.get("lstcarAt") or "") == "1",
        "position_status": POSITION_STATUS.get(
            str(row.get("trainSttus") or "").strip(), ""
        ),
        "reception_dt": reception_dt,
        "age_sec": _age_sec(reception_dt, now),
    }


def _normalize_arrival(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    reception_dt = _parse_reception_dt(row.get("recptnDt"))
    station_raw = str(row.get("statnNm") or "")
    return {
        "kind": "arrival",
        "subway_id": str(row.get("subwayId") or ""),
        "line": line_from_subway_id(row.get("subwayId")),
        "train_no": str(row.get("btrainNo") or ""),
        "station_id": str(row.get("statnId") or ""),
        "station_name": normalize_station(station_raw),
        "station_name_raw": station_raw,
        "direction": _direction(row.get("updnLine")),
        # 도착 응답에는 directAt 이 아예 없다. 급행 여부는 btrainSttus('일반'/'급행').
        "express": _is_express(row.get("btrainSttus") or row.get("directAt")),
        "last_train": str(row.get("lstcarAt") or "") == "1",
        "terminal_station": _terminal_name(row.get("bstatnNm")),
        "eta_sec": _to_int(row.get("barvlDt")),
        "arrival_message": str(row.get("arvlMsg2") or ""),
        "arrival_code": str(row.get("arvlCd") or ""),
        "reception_dt": reception_dt,
        "age_sec": _age_sec(reception_dt, now),
    }


def normalize_rows(rows: Iterable[dict[str, Any]], kind: str, now: datetime) -> list[dict[str, Any]]:
    """원시 행을 라우터·예측 엔진이 쓰는 snake_case 레코드로 변환한다."""
    normalizer = _normalize_position if kind == "position" else _normalize_arrival
    return [normalizer(row, now) for row in rows]


def _safe_component(text: str) -> str:
    """스냅샷 파일명에 쓸 수 있게 경로 구분자를 제거한다."""
    cleaned = "".join(c for c in str(text) if c not in '\\/:*?"<>|').strip()
    return cleaned or "unknown"


class RealtimeClient:
    """실시간 위치/도착 조회기. 실패하면 조용히 replay 로 내려간다."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        con: Any | None = None,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
        start: int = DEFAULT_START,
        end: int = DEFAULT_END,
    ) -> None:
        self._settings = settings
        self._base_url = base_url.rstrip("/")
        self._con = con
        # 키가 없으면 네트워크 자체를 만들지 않는다. 테스트에서 "호출 0회"를 보장한다.
        self._client = client
        self._owns_client = False
        if self._client is None and settings.realtime_enabled:
            self._client = httpx.Client(timeout=timeout)
            self._owns_client = True
        self._clock = clock
        self._now = now
        self._start = start
        self._end = end
        self._cache: dict[tuple[str, str], tuple[float, RealtimeResult]] = {}
        # replay 는 호출할 때마다 다음 스냅샷으로 넘어간다. 데모에서 화면이 멈춰 보이지 않도록.
        # (kind, key) 별로 따로 돈다. 노선마다 스냅샷 개수가 달라 커서를 공유하면 어긋난다.
        self._replay_cursor: dict[tuple[str, str], int] = {}

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()

    def __enter__(self) -> "RealtimeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- public API --------------------------------------------------------
    def fetch_positions(self, line: str) -> RealtimeResult:
        """노선의 실시간 열차 위치."""
        normalized = normalize_line(line)
        return self._fetch("position", normalized or str(line or ""), normalized or str(line or ""))

    def fetch_arrivals(self, station: str) -> RealtimeResult:
        """역의 실시간 도착 정보."""
        raw = str(station or "").strip()
        # 캐시 키는 정규화된 역명으로 잡아야 '강남'/'강남역'이 같은 항목을 공유한다.
        return self._fetch("arrival", normalize_station(raw) or raw, raw)

    def fetch_all_arrivals(self) -> RealtimeResult:
        """전 역 도착 정보를 한 번에. 호출 한도를 아끼는 경로다."""
        return self._fetch("arrival", ARRIVAL_ALL_KEY, ARRIVAL_ALL_KEY)

    def save_snapshot(self, result: RealtimeResult, *, directory: Path | None = None) -> Path | None:
        """live 응답을 스냅샷으로 남긴다. 키 없이도 데모를 돌리기 위한 녹화 기능.

        replay 결과(payload 없음)는 저장하지 않는다. 재저장하면 같은 관측이 증식한다.
        """
        if result.payload is None:
            return None
        target_dir = Path(directory or self._settings.snapshot_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = result.fetched_at.strftime("%Y%m%d-%H%M%S-%f")
        path = target_dir / f"{result.kind}_{_safe_component(result.key)}_{stamp}.json"
        document = {
            "kind": result.kind,
            "key": result.key,
            "saved_at": result.fetched_at.isoformat(),
            "payload": result.payload,
        }
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    # -- internals ---------------------------------------------------------
    def _fetch(self, kind: str, cache_key: str, api_arg: str) -> RealtimeResult:
        cached = self._cache_get(kind, cache_key)
        if cached is not None:
            return cached

        now = self._now()
        if self._client is not None and self._settings.realtime_enabled:
            try:
                payload = self._request(kind, api_arg)
                rows = _unwrap(payload, kind)
            except (RealtimeApiError, httpx.HTTPError):
                # 인증키 미승인(ERROR-338)·네트워크 장애 모두 여기로 모인다.
                # 서비스가 멈추는 것보다 과거 스냅샷을 보여주는 편이 낫다.
                result = self._replay(kind, cache_key, now)
            else:
                result = RealtimeResult(
                    source=SOURCE_LIVE,
                    kind=kind,
                    key=cache_key,
                    fetched_at=now,
                    records=normalize_rows(rows, kind, now),
                    payload=payload,
                )
                self._persist(result)
        else:
            result = self._replay(kind, cache_key, now)

        self._cache_put(kind, cache_key, result)
        return result

    def _request(self, kind: str, api_arg: str) -> dict[str, Any]:
        assert self._client is not None  # _fetch 에서 이미 확인
        service = SERVICE_BY_KIND[kind]
        parts = [self._base_url, str(self._settings.realtime_api_key), "json", service]
        if kind == "arrival" and api_arg == ARRIVAL_ALL_KEY:
            # 전체 조회만 인자 순서가 다르다: .../realtimeStationArrival/ALL/{START}/{END}/
            parts.extend([ARRIVAL_ALL_KEY, str(self._start), str(self._end), ""])
        else:
            parts.extend([str(self._start), str(self._end), api_arg])
        response = self._client.get("/".join(parts))
        response.raise_for_status()
        return response.json()

    # -- cache -------------------------------------------------------------
    def _cache_get(self, kind: str, key: str) -> RealtimeResult | None:
        entry = self._cache.get((kind, key))
        if entry is None:
            return None
        stored_at, result = entry
        if self._clock() - stored_at >= self._settings.realtime_cache_ttl_sec:
            del self._cache[(kind, key)]
            return None
        return result

    def _cache_put(self, kind: str, key: str, result: RealtimeResult) -> None:
        self._cache[(kind, key)] = (self._clock(), result)

    # -- persistence -------------------------------------------------------
    def _persist(self, result: RealtimeResult) -> None:
        """live 관측만 로그 테이블에 적재한다. 연결이 없으면 조용히 건너뛴다."""
        if self._con is None or not result.is_live or not result.records:
            return
        if result.kind == "position":
            self._con.executemany(
                "INSERT INTO train_position_log"
                " (subway_id, train_no, station_id, station_name, direction,"
                "  express_yn, terminal_station, position_status, reception_dt, collected_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    [
                        r["subway_id"],
                        r["train_no"],
                        r["station_id"],
                        r["station_name"],
                        r["direction"],
                        r["express"],
                        r["terminal_station"],
                        r["position_status"],
                        r["reception_dt"],
                        result.fetched_at,
                    ]
                    for r in result.records
                ],
            )
        else:
            self._con.executemany(
                "INSERT INTO arrival_log"
                " (subway_id, station_id, station_name, train_no, arrival_eta_sec,"
                "  express_yn, terminal_station, direction, collected_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    [
                        r["subway_id"],
                        r["station_id"],
                        r["station_name"],
                        r["train_no"],
                        r["eta_sec"],
                        r["express"],
                        r["terminal_station"],
                        r["direction"],
                        result.fetched_at,
                    ]
                    for r in result.records
                ],
            )

    # -- replay ------------------------------------------------------------
    def _snapshot_files(self, kind: str, key: str) -> list[Path]:
        """요청한 노선/역의 스냅샷만 고른다. 이름순이라 순회 순서가 결정적이다.

        파일명은 save_snapshot 이 만든 `{kind}_{key}_{timestamp}.json` 이다.
        key 를 무시하고 kind 만으로 고르면 2호선을 요청했는데 1호선 스냅샷이
        재생된다. 다른 노선 열차를 그 노선인 척 보여주느니 아무것도 안 보이는 편이 낫다.
        """
        directory = Path(self._settings.snapshot_dir)
        if not directory.is_dir():
            return []
        prefix = f"{kind}_{_safe_component(key)}_"
        return sorted(
            p for p in directory.glob(f"{prefix}*.json") if p.is_file()
        )

    def _replay(self, kind: str, key: str, now: datetime) -> RealtimeResult:
        """스냅샷을 순환 재생한다. 스냅샷이 없거나 깨져도 예외를 내지 않는다."""
        files = self._snapshot_files(kind, key)
        records: list[dict[str, Any]] = []
        if files:
            cursor_key = (kind, key)
            cursor = self._replay_cursor.get(cursor_key, 0)
            # 파일 하나가 깨져 있어도 데모가 멈추면 안 되므로 한 바퀴까지 돌며 찾는다.
            for offset in range(len(files)):
                path = files[(cursor + offset) % len(files)]
                rows = self._read_snapshot(path, kind)
                if rows is not None:
                    records = normalize_rows(rows, kind, now)
                    cursor = cursor + offset + 1
                    break
            else:
                cursor = cursor + 1
            self._replay_cursor[cursor_key] = cursor % len(files)

        return RealtimeResult(
            source=SOURCE_REPLAY,
            kind=kind,
            key=key,
            fetched_at=now,
            records=records,
            payload=None,
        )

    @staticmethod
    def _read_snapshot(path: Path, kind: str) -> list[dict[str, Any]] | None:
        """스냅샷 1개에서 원시 행을 꺼낸다. 해석 불가면 None."""
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(document, dict):
            return None
        # save_snapshot 이 만든 문서면 payload 안에, 손으로 넣은 원시 응답이면 최상위에 있다.
        payload = document.get("payload")
        if not isinstance(payload, dict):
            payload = document
        try:
            return _unwrap(payload, kind)
        except RealtimeApiError:
            return None
