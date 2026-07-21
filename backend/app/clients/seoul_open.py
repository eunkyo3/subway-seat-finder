"""서울 열린데이터광장 일반 OpenAPI 클라이언트.

엔드포인트: http://openapi.seoul.go.kr:8088/{KEY}/json/{SERVICE}/{START}/{END}/{args...}

응답 규약 (실측 확인):
- 정상        : {"<SERVICE>": {"list_total_count": N, "RESULT": {"CODE": "INFO-000"}, "row": [...]}}
- 데이터 없음  : {"RESULT": {"CODE": "INFO-200"}}
- 인자 누락    : {"RESULT": {"CODE": "ERROR-300"}}
- 서비스명 오류: {"RESULT": {"CODE": "ERROR-500"}}
1회 응답 최대 1,000행이라 그보다 크면 페이지를 나눠 호출해야 한다.
"""

from __future__ import annotations

from typing import Any, Iterator

import httpx

BASE_URL = "http://openapi.seoul.go.kr:8088"
PAGE_SIZE = 1000


class SeoulOpenApiError(RuntimeError):
    """정상(INFO-000)이 아닌 응답."""

    def __init__(self, code: str, message: str, service: str) -> None:
        super().__init__(f"[{service}] {code}: {message}")
        self.code = code
        self.message = message
        self.service = service


class NoDataError(SeoulOpenApiError):
    """INFO-200 — 조건에 맞는 데이터가 없음. 호출 자체는 성공."""


def _unwrap(payload: dict[str, Any], service: str) -> tuple[list[dict], int]:
    """응답 봉투를 벗겨 (rows, total) 를 돌려준다."""
    body = payload.get(service)
    if body is None:
        # 오류 응답은 서비스 키 없이 RESULT 만 온다.
        result = payload.get("RESULT", {})
        code = result.get("CODE", "UNKNOWN")
        message = result.get("MESSAGE", "응답 형식을 해석할 수 없습니다.")
        if code == "INFO-200":
            raise NoDataError(code, message, service)
        raise SeoulOpenApiError(code, message, service)

    result = body.get("RESULT", {})
    code = result.get("CODE", "UNKNOWN")
    if code != "INFO-000":
        if code == "INFO-200":
            raise NoDataError(code, result.get("MESSAGE", ""), service)
        raise SeoulOpenApiError(code, result.get("MESSAGE", ""), service)

    return body.get("row", []) or [], int(body.get("list_total_count", 0))


class SeoulOpenClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("일반 인증키가 없습니다. api-key.txt 또는 SEOUL_API_KEY 를 설정하세요.")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "SeoulOpenClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch_page(
        self, service: str, start: int, end: int, *args: str
    ) -> tuple[list[dict], int]:
        parts = [self._base_url, self._api_key, "json", service, str(start), str(end)]
        parts.extend(str(a) for a in args if a)
        response = self._client.get("/".join(parts))
        response.raise_for_status()
        return _unwrap(response.json(), service)

    def fetch_all(
        self, service: str, *args: str, page_size: int = PAGE_SIZE, max_rows: int | None = None
    ) -> Iterator[dict]:
        """전체 행을 페이지네이션으로 순회한다. 데이터가 없으면 조용히 종료한다."""
        start = 1
        total: int | None = None
        emitted = 0
        while True:
            end = start + page_size - 1
            try:
                rows, count = self.fetch_page(service, start, end, *args)
            except NoDataError:
                return
            if total is None:
                total = count
            if not rows:
                return
            for row in rows:
                yield row
                emitted += 1
                if max_rows is not None and emitted >= max_rows:
                    return
            if total and end >= total:
                return
            start = end + 1
