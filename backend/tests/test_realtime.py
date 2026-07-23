"""실시간 클라이언트 테스트.

실시간 인증키가 없는 환경에서도 전부 통과해야 한다. 네트워크는 httpx.MockTransport 로
주입하고, TTL 만료는 clock 콜러블을 주입해 sleep 없이 앞당긴다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import duckdb
import httpx
import pytest

from backend.app.clients.realtime import (
    RealtimeClient,
    RealtimeResult,
    _unwrap,
)
from backend.app.config import Settings
from backend.app.db import init_schema

# 원천 생성시각과 "지금"을 고정해 age_sec 를 결정적으로 검증한다.
NOW = datetime(2026, 7, 21, 9, 0, 20)

# 아래 두 봉투는 실제 실시간 API 응답을 그대로 옮긴 것이다(2026-07-22 실측).
# 위치와 도착이 서로 다른 필드명을 쓴다는 점이 핵심이다.
#   위치: trainNo / statnTnm('성수종착') / directAt / updnLine 은 코드('0','1')
#   도착: btrainNo / bstatnNm / btrainSttus('일반','급행') / updnLine 은 한글('상행','외선')
# 추측으로 만든 픽스처는 이 차이를 못 잡아 통과해 버린다.
POSITION_PAYLOAD = {
    "errorMessage": {
        "status": 200,
        "code": "INFO-000",
        "message": "정상 처리되었습니다.",
        "total": 2,
    },
    "realtimePositionList": [
        {
            "subwayId": "1002",
            "subwayNm": "2호선",
            "statnId": "1002000230",
            "statnNm": "강남",
            "trainNo": "6508",
            "lastRecptnDt": "20260722",
            "recptnDt": "2026-07-21 09:00:00",
            "updnLine": "0",
            "statnTid": "1002000211",
            "statnTnm": "성수종착",
            "trainSttus": "1",
            "directAt": "0",
            "lstcarAt": "0",
        },
        {
            "subwayId": "1002",
            "subwayNm": "2호선",
            "statnId": "1002000201",
            "statnNm": "시청역",
            "trainNo": "2101",
            "lastRecptnDt": "20260722",
            "recptnDt": "2026-07-21 08:59:30",
            "updnLine": "1",
            "statnTid": "1002002114",
            "statnTnm": "신설동행",
            "trainSttus": "3",
            "directAt": "1",
            "lstcarAt": "1",
        },
    ],
}

ARRIVAL_PAYLOAD = {
    "errorMessage": {"status": 200, "code": "INFO-000", "message": "정상", "total": 1},
    "realtimeArrivalList": [
        {
            "subwayId": "1002",
            "updnLine": "상행",
            "trainLineNm": "성수행 - 역삼방면",
            "statnFid": "1002000221",
            "statnTid": "1002000223",
            "statnId": "1002000222",
            "statnNm": "강남",
            "trnsitCo": "2",
            "ordkey": "01000성수0",
            "subwayList": "1002",
            "statnList": "1002000222",
            "btrainSttus": "일반",
            "barvlDt": "90",
            "btrainNo": "2234",
            "bstatnId": "1002000211",
            "bstatnNm": "성수",
            "recptnDt": "2026-07-21 09:00:10",
            "arvlMsg2": "전역 출발",
            "arvlMsg3": "역삼",
            "arvlCd": "3",
            "lstcarAt": "0",
        }
    ],
}

# 일반 인증키로 실시간 엔드포인트를 때렸을 때 실측된 오류 봉투.
ERROR_338_PAYLOAD = {
    "status": 500,
    "code": "ERROR-338",
    "message": "해당 인증키로는 실시간 서비스를 사용할 수 없습니다.",
    "total": 0,
}


def arrival_payload_with(**overrides) -> dict:
    """ARRIVAL_PAYLOAD 의 첫 행 필드만 바꾼 깊은 복사본."""
    payload = json.loads(json.dumps(ARRIVAL_PAYLOAD))
    payload["realtimeArrivalList"][0].update(overrides)
    return payload


def make_settings(tmp_path: Path, *, key: str | None = "RT-KEY", ttl: int = 30) -> Settings:
    return Settings(
        api_key="GENERAL-KEY",
        realtime_api_key=key,
        db_path=tmp_path / "subway.duckdb",
        raw_dir=tmp_path / "raw",
        snapshot_dir=tmp_path / "snapshots",
        realtime_cache_ttl_sec=ttl,
        similar_threshold_pct=8.0,
    )


class CallCounter:
    """MockTransport 핸들러 + 호출 횟수 계수기. 캐시 검증의 근거가 된다."""

    def __init__(self, payload: dict | None = None, *, exc: Exception | None = None) -> None:
        self.payload = payload
        self.exc = exc
        self.count = 0
        self.urls: list[str] = []
        # 한글 인자는 str(url) 에서 퍼센트 인코딩되므로 디코딩된 path 를 따로 모은다.
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.count += 1
        self.urls.append(str(request.url))
        self.paths.append(request.url.path)
        if self.exc is not None:
            raise self.exc
        return httpx.Response(200, json=self.payload)


def make_client(
    settings: Settings,
    handler: CallCounter | None = None,
    *,
    con=None,
    clock=None,
    now=NOW,
) -> RealtimeClient:
    http_client = None
    if handler is not None:
        http_client = httpx.Client(transport=httpx.MockTransport(handler))
    return RealtimeClient(
        settings,
        client=http_client,
        con=con,
        clock=clock or (lambda: 0.0),
        now=lambda: now,
    )


def write_snapshot(
    settings: Settings,
    kind: str,
    payload: dict,
    name: str = "a",
    *,
    key: str = "2호선",
) -> Path:
    """save_snapshot 과 같은 이름 규칙으로 스냅샷을 심는다.

    파일명은 `{kind}_{key}_{stamp}.json` 이어야 한다. 재생이 이 이름으로 노선을
    가려내므로, 여기서 규칙이 어긋나면 테스트가 실제 동작과 다른 것을 검증하게 된다.
    """
    settings.snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = settings.snapshot_dir / f"{kind}_{key}_{name}.json"
    path.write_text(
        json.dumps({"kind": kind, "key": key, "payload": payload}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


class TestOwnClientConfig:
    def test_own_client_follows_redirects(self, tmp_path):
        # ALL 도착 조회가 307 로 미리 생성된 JSON 파일을 가리킨다(실측).
        # 이 설정이 빠지면 도착 수집 전체가 조용히 replay 로 내려간다.
        with RealtimeClient(make_settings(tmp_path)) as client:
            assert client._client is not None
            assert client._client.follow_redirects is True


class TestLiveSuccess:
    def test_positions_are_normalized(self, tmp_path):
        handler = CallCounter(POSITION_PAYLOAD)
        with make_client(make_settings(tmp_path), handler) as client:
            result = client.fetch_positions("2호선")

        assert isinstance(result, RealtimeResult)
        assert result.source == "live"
        assert result.kind == "position"
        assert len(result.records) == 2

        first = result.records[0]
        assert first["line"] == "2호선"  # subwayId 1002 -> 노선명
        assert first["station_name"] == "강남"
        assert first["train_no"] == "6508"  # trainNo, btrainNo 가 아니다
        assert first["position_status"] == "도착"
        assert first["direction"] == "상선"
        assert first["express"] is False
        assert first["terminal_station"] == "성수"

        second = result.records[1]
        assert second["station_name"] == "시청"  # '역' 접미사 정규화
        assert second["position_status"] == "전역출발"
        assert second["direction"] == "하선"
        assert second["express"] is True

    def test_arrivals_are_normalized(self, tmp_path):
        handler = CallCounter(ARRIVAL_PAYLOAD)
        with make_client(make_settings(tmp_path), handler) as client:
            result = client.fetch_arrivals("강남역")

        assert result.source == "live"
        record = result.records[0]
        # barvlDt=90 은 recptnDt(09:00:10) 기준이다. NOW(09:00:20)까지 10초가
        # 흘렀으므로 서빙 ETA 는 80초 — 나이를 안 빼면 시간이 멈춘 카운트다운이 된다.
        assert record["eta_sec"] == 80 and isinstance(record["eta_sec"], int)
        assert record["arrival_message"] == "전역 출발"
        assert record["station_name"] == "강남"
        assert record["line"] == "2호선"

    def test_request_url_targets_realtime_host(self, tmp_path):
        handler = CallCounter(POSITION_PAYLOAD)
        with make_client(make_settings(tmp_path), handler) as client:
            client.fetch_positions("02호선")

        assert "swopenapi.seoul.go.kr" in handler.urls[0]
        path = handler.paths[0]
        assert "/RT-KEY/json/realtimePosition/" in path
        assert path.endswith("2호선")  # 정규화된 노선명이 인자로 나간다

    def test_all_arrivals_uses_all_path(self, tmp_path):
        handler = CallCounter(ARRIVAL_PAYLOAD)
        with make_client(make_settings(tmp_path), handler) as client:
            result = client.fetch_all_arrivals()

        assert result.source == "live"
        assert "/json/realtimeStationArrival/ALL/0/100/" in handler.paths[0]


class TestFieldNameDifferences:
    """위치와 도착이 같은 개념을 다른 필드명으로 준다. 여기가 틀리면 조용히 망가진다."""

    def test_position_train_number_comes_from_trainNo(self, tmp_path):
        # btrainNo 를 읽으면 열차번호가 통째로 비고, 시발 감지·배차간격이 죽는다.
        handler = CallCounter(POSITION_PAYLOAD)
        with make_client(make_settings(tmp_path), handler) as client:
            records = client.fetch_positions("2호선").records
        assert all(r["train_no"] for r in records)
        assert records[0]["train_no"] == POSITION_PAYLOAD["realtimePositionList"][0]["trainNo"]

    def test_arrival_train_number_comes_from_btrainNo(self, tmp_path):
        handler = CallCounter(ARRIVAL_PAYLOAD)
        with make_client(make_settings(tmp_path), handler) as client:
            records = client.fetch_arrivals("강남").records
        assert records[0]["train_no"] == ARRIVAL_PAYLOAD["realtimeArrivalList"][0]["btrainNo"]

    def test_position_terminal_comes_from_statnTnm_with_suffix_stripped(self, tmp_path):
        # 위치는 '성수종착'/'신설동행' 처럼 꼬리를 붙여 준다. 도착의 '성수' 와 맞춰야 한다.
        handler = CallCounter(POSITION_PAYLOAD)
        with make_client(make_settings(tmp_path), handler) as client:
            records = client.fetch_positions("2호선").records
        assert records[0]["terminal_station"] == "성수"
        assert records[1]["terminal_station"] == "신설동"

    def test_position_and_arrival_terminals_agree(self, tmp_path):
        settings = make_settings(tmp_path)
        with make_client(settings, CallCounter(POSITION_PAYLOAD)) as client:
            pos = client.fetch_positions("2호선").records[0]
        with make_client(settings, CallCounter(ARRIVAL_PAYLOAD)) as client:
            arr = client.fetch_arrivals("강남").records[0]
        # '성수종착'(위치) 과 '성수'(도착) 가 같은 값으로 떨어져야 조인이 된다.
        assert pos["terminal_station"] == arr["terminal_station"]

    def test_arrival_express_comes_from_btrainSttus(self, tmp_path):
        # 도착 응답에는 directAt 이 아예 없다.
        payload = json.loads(json.dumps(ARRIVAL_PAYLOAD))
        assert "directAt" not in payload["realtimeArrivalList"][0]

        payload["realtimeArrivalList"][0]["btrainSttus"] = "급행"
        with make_client(make_settings(tmp_path), CallCounter(payload)) as client:
            assert client.fetch_arrivals("강남").records[0]["express"] is True

        payload["realtimeArrivalList"][0]["btrainSttus"] = "일반"
        with make_client(make_settings(tmp_path), CallCounter(payload)) as client:
            assert client.fetch_arrivals("강남").records[0]["express"] is False

    def test_position_express_comes_from_directAt(self, tmp_path):
        handler = CallCounter(POSITION_PAYLOAD)
        with make_client(make_settings(tmp_path), handler) as client:
            records = client.fetch_positions("2호선").records
        assert records[0]["express"] is False   # directAt "0"
        assert records[1]["express"] is True    # directAt "1"

    def test_direction_is_unified_across_both_endpoints(self, tmp_path):
        # 위치는 코드('0'), 도착은 한글('상행')로 준다. 둘 다 '상선' 이어야 한다.
        settings = make_settings(tmp_path)
        with make_client(settings, CallCounter(POSITION_PAYLOAD)) as client:
            pos = client.fetch_positions("2호선").records[0]
        with make_client(settings, CallCounter(ARRIVAL_PAYLOAD)) as client:
            arr = client.fetch_arrivals("강남").records[0]
        assert pos["direction"] == arr["direction"] == "상선"

    def test_last_train_flag(self, tmp_path):
        handler = CallCounter(POSITION_PAYLOAD)
        with make_client(make_settings(tmp_path), handler) as client:
            records = client.fetch_positions("2호선").records
        assert records[0]["last_train"] is False
        assert records[1]["last_train"] is True


class TestAgeCorrection:
    def test_age_sec_is_now_minus_reception_dt(self, tmp_path):
        handler = CallCounter(POSITION_PAYLOAD)
        with make_client(make_settings(tmp_path), handler) as client:
            records = client.fetch_positions("2호선").records

        assert records[0]["reception_dt"] == datetime(2026, 7, 21, 9, 0, 0)
        assert records[0]["age_sec"] == 20.0
        assert records[1]["age_sec"] == 50.0

    def test_future_reception_dt_clamps_to_zero(self, tmp_path):
        # 원천/수집 서버 시계 오차로 미래 시각이 오면 음수 나이가 나온다.
        payload = json.loads(json.dumps(POSITION_PAYLOAD))
        payload["realtimePositionList"][0]["recptnDt"] = "2026-07-21 09:00:50"
        handler = CallCounter(payload)
        with make_client(make_settings(tmp_path), handler) as client:
            records = client.fetch_positions("2호선").records

        assert records[0]["age_sec"] == 0.0

    def test_missing_reception_dt_yields_none(self, tmp_path):
        payload = json.loads(json.dumps(POSITION_PAYLOAD))
        payload["realtimePositionList"][0]["recptnDt"] = ""
        handler = CallCounter(payload)
        with make_client(make_settings(tmp_path), handler) as client:
            records = client.fetch_positions("2호선").records

        assert records[0]["reception_dt"] is None
        assert records[0]["age_sec"] is None


class TestFallback:
    def test_error_338_falls_back_to_replay(self, tmp_path):
        settings = make_settings(tmp_path)
        write_snapshot(settings, "position", POSITION_PAYLOAD)
        handler = CallCounter(ERROR_338_PAYLOAD)

        with make_client(settings, handler) as client:
            result = client.fetch_positions("2호선")

        assert handler.count == 1  # 시도는 했다
        assert result.source == "replay"
        assert len(result.records) == 2
        assert result.payload is None

    def test_missing_key_never_touches_network(self, tmp_path):
        settings = make_settings(tmp_path, key=None)
        write_snapshot(settings, "position", POSITION_PAYLOAD)
        handler = CallCounter(POSITION_PAYLOAD)

        with make_client(settings, handler) as client:
            result = client.fetch_positions("2호선")

        assert handler.count == 0  # 한도 소모는커녕 소켓도 열지 않는다
        assert result.source == "replay"
        assert result.records[0]["station_name"] == "강남"

    def test_network_exception_falls_back(self, tmp_path):
        settings = make_settings(tmp_path)
        write_snapshot(settings, "arrival", ARRIVAL_PAYLOAD, key="강남")
        handler = CallCounter(exc=httpx.ConnectTimeout("timeout"))

        with make_client(settings, handler) as client:
            result = client.fetch_arrivals("강남")

        assert result.source == "replay"
        assert result.records[0]["eta_sec"] == 90

    def test_http_status_error_falls_back(self, tmp_path):
        settings = make_settings(tmp_path)
        write_snapshot(settings, "position", POSITION_PAYLOAD)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="service unavailable")

        client = RealtimeClient(
            settings,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            clock=lambda: 0.0,
            now=lambda: NOW,
        )
        with client:
            result = client.fetch_positions("2호선")

        assert result.source == "replay"

    def test_unexpected_envelope_falls_back(self, tmp_path):
        # 목록 키가 없는 응답. 파싱 실패도 호출자에게 예외로 새면 안 된다.
        settings = make_settings(tmp_path)
        handler = CallCounter({"errorMessage": {"code": "INFO-000"}})

        with make_client(settings, handler) as client:
            result = client.fetch_positions("2호선")

        assert result.source == "replay"
        assert result.records == []


class TestTtlCache:
    def test_second_call_within_ttl_skips_network(self, tmp_path):
        clock = {"t": 0.0}
        handler = CallCounter(POSITION_PAYLOAD)
        settings = make_settings(tmp_path, ttl=30)

        with make_client(settings, handler, clock=lambda: clock["t"]) as client:
            first = client.fetch_positions("2호선")
            clock["t"] = 29.0
            second = client.fetch_positions("2호선")

        assert handler.count == 1
        assert second is first  # 캐시가 같은 객체를 돌려준다

    def test_call_after_ttl_expiry_refetches(self, tmp_path):
        clock = {"t": 0.0}
        handler = CallCounter(POSITION_PAYLOAD)
        settings = make_settings(tmp_path, ttl=30)

        with make_client(settings, handler, clock=lambda: clock["t"]) as client:
            client.fetch_positions("2호선")
            clock["t"] = 31.0
            client.fetch_positions("2호선")

        assert handler.count == 2

    def test_cache_key_separates_kind_and_target(self, tmp_path):
        handler = CallCounter(POSITION_PAYLOAD)
        settings = make_settings(tmp_path, ttl=30)

        with make_client(settings, handler) as client:
            client.fetch_positions("2호선")
            client.fetch_positions("3호선")  # 다른 노선 -> 별도 항목

        assert handler.count == 2

    def test_unnormalized_station_shares_cache_entry(self, tmp_path):
        handler = CallCounter(ARRIVAL_PAYLOAD)
        settings = make_settings(tmp_path, ttl=30)

        with make_client(settings, handler) as client:
            client.fetch_arrivals("강남역")
            client.fetch_arrivals("강남")

        assert handler.count == 1


class TestPersistence:
    @pytest.fixture()
    def con(self):
        connection = duckdb.connect(":memory:")
        init_schema(connection)
        yield connection
        connection.close()

    def test_live_positions_are_appended(self, tmp_path, con):
        handler = CallCounter(POSITION_PAYLOAD)
        with make_client(make_settings(tmp_path), handler, con=con) as client:
            client.fetch_positions("2호선")

        rows = con.execute(
            "SELECT train_no, station_name, express_yn, position_status,"
            " reception_dt, collected_at FROM train_position_log ORDER BY train_no"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "2101"
        assert rows[0][2] is True  # 급행
        assert rows[1][1] == "강남"
        assert rows[1][4] == datetime(2026, 7, 21, 9, 0, 0)
        assert rows[1][5] == NOW

    def test_live_arrivals_are_appended(self, tmp_path, con):
        handler = CallCounter(ARRIVAL_PAYLOAD)
        with make_client(make_settings(tmp_path), handler, con=con) as client:
            client.fetch_arrivals("강남")

        rows = con.execute(
            "SELECT station_name, arrival_eta_sec, direction FROM arrival_log"
        ).fetchall()
        assert rows == [("강남", 90, "상선")]

    def test_replay_is_not_persisted(self, tmp_path, con):
        # replay 는 새 관측이 아니다. 저장하면 과거 데이터가 현재 시각으로 위조된다.
        settings = make_settings(tmp_path)
        write_snapshot(settings, "position", POSITION_PAYLOAD)
        handler = CallCounter(ERROR_338_PAYLOAD)

        with make_client(settings, handler, con=con) as client:
            result = client.fetch_positions("2호선")

        assert result.source == "replay" and result.records
        assert con.execute("SELECT count(*) FROM train_position_log").fetchone()[0] == 0

    def test_no_connection_warns_once_and_skips_persistence(self, tmp_path, caplog):
        # 조회 자체는 성공해야 하지만, live 관측을 버린다는 사실은 숨기면 안 된다.
        # 이 침묵이 §A(시발 보정 무효)를 오래 숨겼다. 경고는 호출마다가 아니라
        # 인스턴스당 1회 — 30초 TTL 로 하루 종일 도는 앱에서 로그가 잠기면 안 된다.
        handler = CallCounter(POSITION_PAYLOAD)
        with caplog.at_level(logging.WARNING, logger="backend.app.clients.realtime"):
            with make_client(make_settings(tmp_path), handler, con=None) as client:
                result = client.fetch_positions("2호선")
                client.fetch_positions("3호선")

        assert result.source == "live" and len(result.records) == 2
        warnings = [r for r in caplog.records if "시발" in r.getMessage()]
        assert len(warnings) == 1


class TestArrivalEtaDisambiguation:
    """barvlDt=0 은 '도착 직전'과 '카운트다운 미상'을 겸한다 (실측: 하루치 로그의
    59%가 0). arvlCd 로 갈라 미상은 None 으로 둬야 후보 정렬(0 은 falsy)·
    배차간격·캘리브레이션 표본이 오염되지 않는다."""

    def _record(self, tmp_path, **overrides):
        handler = CallCounter(arrival_payload_with(**overrides))
        with make_client(make_settings(tmp_path), handler) as client:
            records = client.fetch_arrivals("강남").records
        return records[0] if records else None

    def test_zero_with_countdown_unknown_code_is_none(self, tmp_path):
        record = self._record(tmp_path, barvlDt="0", arvlCd="99")
        assert record["eta_sec"] is None

    def test_zero_while_arriving_is_genuine_zero(self, tmp_path):
        record = self._record(tmp_path, barvlDt="0", arvlCd="1")
        assert record["eta_sec"] == 0

    def test_numeric_zero_while_approaching_is_genuine_zero(self, tmp_path):
        # 스냅샷을 손으로 만들면 barvlDt 가 숫자 0 으로 올 수 있다.
        # falsy 라고 결측 취급하면 도착 직전 열차가 사라진다.
        record = self._record(tmp_path, barvlDt=0, arvlCd="0")
        assert record["eta_sec"] == 0

    def test_missing_barvlDt_is_none(self, tmp_path):
        record = self._record(tmp_path, barvlDt="", arvlCd="3")
        assert record["eta_sec"] is None


class TestStaleArrivalGhosts:
    """운행 종료 후 원천이 recptnDt 갱신을 멈추면, 열차가 안 오는데도 오래된
    '몇 분 후 도착'이 남는다(유령 도착). live 서빙은 데이터 나이만큼 시간을
    흘려보내고, 이미 지나간 열차와 죽은 원천을 걸러낸다."""

    def test_departed_train_is_dropped(self, tmp_path):
        # 5분 전 기준 '60초 후 도착'. 유예(30초)를 훨씬 넘겨 이미 떠난 열차다.
        payload = arrival_payload_with(barvlDt="60", recptnDt="2026-07-21 08:55:20")
        with make_client(make_settings(tmp_path), CallCounter(payload)) as client:
            assert client.fetch_arrivals("강남").records == []

    def test_dead_source_is_dropped_even_without_eta(self, tmp_path):
        # ETA 미상이라도 원천이 10분 넘게 침묵하면 죽은 데이터다.
        payload = arrival_payload_with(
            barvlDt="0", arvlCd="99", recptnDt="2026-07-21 08:40:00"
        )
        with make_client(make_settings(tmp_path), CallCounter(payload)) as client:
            assert client.fetch_arrivals("강남").records == []

    def test_slightly_late_record_is_clamped_not_dropped(self, tmp_path):
        # 유예 안쪽의 음수는 시계 오차·정차 시간일 수 있어 '지금 도착'으로 접는다.
        payload = arrival_payload_with(barvlDt="5", recptnDt="2026-07-21 09:00:00")
        with make_client(make_settings(tmp_path), CallCounter(payload)) as client:
            (record,) = client.fetch_arrivals("강남").records
        assert record["eta_sec"] == 0

    def test_replay_is_not_age_filtered(self, tmp_path):
        # 재생은 과거 시점을 그대로 보여주는 게 목적이다. 나이를 반영하면
        # 스냅샷 전체가 만료돼 데모가 빈 화면이 된다.
        settings = make_settings(tmp_path)
        write_snapshot(settings, "arrival", ARRIVAL_PAYLOAD, key="강남")
        with make_client(settings, CallCounter(ERROR_338_PAYLOAD)) as client:
            result = client.fetch_arrivals("강남")
        assert result.source == "replay"
        assert result.records and result.records[0]["eta_sec"] == 90

    def test_raw_eta_and_arrival_code_are_persisted(self, tmp_path):
        # 로그에는 보정 전 원시 ETA 가 남아야 캘리브레이션이 수집 시각 기준으로
        # 일관되고, arrival_code 가 있어야 eta=0 의 뜻을 사후에 가릴 수 있다.
        con = duckdb.connect(":memory:")
        init_schema(con)
        try:
            with make_client(
                make_settings(tmp_path), CallCounter(ARRIVAL_PAYLOAD), con=con
            ) as client:
                (record,) = client.fetch_arrivals("강남").records
            assert record["eta_sec"] == 80  # 서빙은 나이 보정
            row = con.execute(
                "SELECT arrival_eta_sec, arrival_code FROM arrival_log"
            ).fetchone()
        finally:
            con.close()
        assert row == (90, "3")


class TestReplaySource:
    def test_missing_snapshot_dir_returns_empty(self, tmp_path):
        settings = make_settings(tmp_path, key=None)
        assert not settings.snapshot_dir.exists()

        with make_client(settings) as client:
            result = client.fetch_positions("2호선")

        assert result.source == "replay"
        assert result.records == []

    def test_empty_snapshot_dir_returns_empty(self, tmp_path):
        settings = make_settings(tmp_path, key=None)
        settings.snapshot_dir.mkdir(parents=True)

        with make_client(settings) as client:
            assert client.fetch_arrivals("강남").records == []

    def test_corrupt_snapshot_is_skipped(self, tmp_path):
        settings = make_settings(tmp_path, key=None)
        settings.snapshot_dir.mkdir(parents=True)
        (settings.snapshot_dir / "position_broken.json").write_text("{not json", encoding="utf-8")
        write_snapshot(settings, "position", POSITION_PAYLOAD, name="ok")

        with make_client(settings) as client:
            result = client.fetch_positions("2호선")

        assert result.source == "replay"
        assert len(result.records) == 2

    def test_replay_cycles_through_snapshots_deterministically(self, tmp_path):
        settings = make_settings(tmp_path, key=None, ttl=0)  # TTL 0 -> 매번 새로 재생
        second_payload = json.loads(json.dumps(POSITION_PAYLOAD))
        second_payload["realtimePositionList"] = [
            second_payload["realtimePositionList"][0] | {"trainNo": "9999"}
        ]
        write_snapshot(settings, "position", POSITION_PAYLOAD, name="1")
        write_snapshot(settings, "position", second_payload, name="2")

        with make_client(settings) as client:
            seen = [client.fetch_positions("2호선").records[0]["train_no"] for _ in range(4)]

        assert seen == ["6508", "9999", "6508", "9999"]

    def test_replay_does_not_mix_lines(self, tmp_path):
        # 2호선 스냅샷만 있는데 5호선을 요청하면, 2호선 열차를 5호선인 척
        # 보여주는 대신 빈 결과를 줘야 한다.
        settings = make_settings(tmp_path, key=None)
        write_snapshot(settings, "position", POSITION_PAYLOAD, key="2호선")

        with make_client(settings) as client:
            assert len(client.fetch_positions("2호선").records) == 2
            other = client.fetch_positions("5호선")
        assert other.source == "replay"
        assert other.records == []

    def test_replay_cursors_are_independent_per_line(self, tmp_path):
        settings = make_settings(tmp_path, key=None)
        write_snapshot(settings, "position", POSITION_PAYLOAD, name="1", key="2호선")
        five = json.loads(json.dumps(POSITION_PAYLOAD))
        five["realtimePositionList"] = [
            five["realtimePositionList"][0] | {"trainNo": "5001", "subwayId": "1005"}
        ]
        write_snapshot(settings, "position", five, name="1", key="5호선")

        with make_client(settings) as client:
            # 노선별로 커서가 따로 돌아야 서로의 순번을 밀지 않는다.
            assert client.fetch_positions("5호선").records[0]["train_no"] == "5001"
            assert client.fetch_positions("2호선").records[0]["train_no"] == "6508"
            assert client.fetch_positions("5호선").records[0]["train_no"] == "5001"

    def test_replay_only_reads_matching_kind(self, tmp_path):
        settings = make_settings(tmp_path, key=None)
        write_snapshot(settings, "arrival", ARRIVAL_PAYLOAD, key="강남")

        with make_client(settings) as client:
            assert client.fetch_positions("2호선").records == []
            assert len(client.fetch_arrivals("강남").records) == 1

    def test_replay_computes_age_from_snapshot_reception_dt(self, tmp_path):
        settings = make_settings(tmp_path, key=None)
        write_snapshot(settings, "position", POSITION_PAYLOAD)

        with make_client(settings) as client:
            records = client.fetch_positions("2호선").records

        # 재생이라도 나이는 실제 원천 시각 기준이어야 하류가 신선도를 오판하지 않는다.
        assert records[0]["age_sec"] == 20.0


class TestSaveSnapshot:
    def test_live_result_is_recorded_and_replayable(self, tmp_path):
        settings = make_settings(tmp_path)
        handler = CallCounter(POSITION_PAYLOAD)

        with make_client(settings, handler) as client:
            live = client.fetch_positions("2호선")
            path = client.save_snapshot(live)

        assert path is not None and path.exists()
        assert path.parent == settings.snapshot_dir

        offline = make_settings(tmp_path, key=None)
        with make_client(offline) as client:
            replayed = client.fetch_positions("2호선")

        assert replayed.source == "replay"
        assert [r["train_no"] for r in replayed.records] == ["6508", "2101"]

    def test_replay_result_is_not_recorded(self, tmp_path):
        settings = make_settings(tmp_path, key=None)
        settings.snapshot_dir.mkdir(parents=True)

        with make_client(settings) as client:
            result = client.fetch_positions("2호선")
            assert client.save_snapshot(result) is None

        assert list(settings.snapshot_dir.iterdir()) == []


class TestUnwrap:
    def test_flat_error_envelope_is_detected(self):
        with pytest.raises(Exception) as excinfo:
            _unwrap(ERROR_338_PAYLOAD, "position")
        assert "ERROR-338" in str(excinfo.value)

    def test_nested_error_envelope_is_detected(self):
        payload = {"errorMessage": {"code": "ERROR-500", "message": "서버 오류"}}
        with pytest.raises(Exception) as excinfo:
            _unwrap(payload, "arrival")
        assert "ERROR-500" in str(excinfo.value)

    def test_no_data_is_an_empty_result_not_an_error(self):
        # 막차 이후에는 운행 열차가 정말로 없다. 장애로 처리하면 유령 열차가 뜬다.
        payload = {"errorMessage": {"code": "INFO-200", "message": "데이터 없음"}}
        assert _unwrap(payload, "arrival") == []

    def test_non_dict_rows_are_dropped(self):
        payload = {
            "errorMessage": {"code": "INFO-000"},
            "realtimePositionList": [{"subwayId": "1002"}, None, "junk"],
        }
        assert _unwrap(payload, "position") == [{"subwayId": "1002"}]
