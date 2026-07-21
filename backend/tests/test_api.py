"""API 엔드포인트 테스트.

실시간 인증키가 없는 환경에서도 예측 경로 전체를 검증해야 하므로,
실시간 클라이언트만 가짜로 갈아끼우고 나머지(DB·예측 엔진)는 진짜를 쓴다.
가짜 도착 데이터는 테스트 안에만 있고 앱이 배포하는 데이터가 아니다.
"""

from __future__ import annotations

from datetime import datetime

import duckdb
import pytest
from fastapi.testclient import TestClient

from backend.app.clients.realtime import RealtimeResult
from backend.app.config import Settings
from backend.app.db import init_schema
from backend.app.deps import AppState
from backend.app.main import app


class FakeRealtime:
    """RealtimeClient 의 최소 대역. 반환할 도착·위치 레코드를 직접 심는다."""

    def __init__(self, *, arrivals=None, positions=None, source="live"):
        self._arrivals = arrivals or []
        self._positions = positions or []
        self.source = source
        self.calls: list[tuple[str, str]] = []

    def _result(self, kind, key, records):
        return RealtimeResult(
            source=self.source,
            kind=kind,
            key=key,
            fetched_at=datetime(2026, 7, 21, 8, 15),
            records=records,
            payload=None,
        )

    def fetch_arrivals(self, station):
        self.calls.append(("arrival", station))
        return self._result("arrival", station, self._arrivals)

    def fetch_positions(self, line):
        self.calls.append(("position", line))
        return self._result("position", line, self._positions)

    def close(self):
        pass


def arrival(train_no, eta_sec, *, line="5호선", direction="상선", express=False,
            station="강남", terminal="성수"):
    return {
        "kind": "arrival",
        "train_no": train_no,
        "line": line,
        "subway_id": "1002",
        "station_name": station,
        "station_name_raw": station,
        "station_id": "0222",
        "direction": direction,
        "express": express,
        "terminal_station": terminal,
        "eta_sec": eta_sec,
        "arrival_message": f"{eta_sec}초 후",
        "reception_dt": datetime(2026, 7, 21, 8, 15),
        "age_sec": 5.0,
    }


def position(train_no, station, *, line="5호선"):
    return {
        "kind": "position",
        "train_no": train_no,
        "line": line,
        "subway_id": "1002",
        "station_name": station,
        "station_name_raw": station,
        "station_id": "0222",
        "direction": "상선",
        "express": False,
        "terminal_station": "성수",
        "position_status": "도착",
        "reception_dt": datetime(2026, 7, 21, 8, 15),
        "age_sec": 8.0,
    }


AT = "2026-07-21T08:15:00"  # 화요일 평일 08:00 슬롯. 시드 통계가 이 시간대에만 있다.

LINE_5 = ["시청", "강남", "역삼", "선릉", "삼성", "종합운동장", "잠실", "사당"]


def seed(con):
    for seq, name in enumerate(LINE_5, start=1):
        con.execute(
            "INSERT INTO station_master (station_key, station_id, name, name_norm,"
            " name_eng, line, seq, branch_no, lat, lng, transfer_yn)"
            " VALUES (?,?,?,?,?,?,?,0,?,?,?)",
            [f"5호선|{name}", f"02{seq:02d}", name, name, name.upper(), "5호선", seq,
             37.5 + seq / 100, 127.0 + seq / 100, seq % 2 == 0],
        )
    # 강남이 가장 붐비고 사당으로 갈수록 빠진다 — 착석 타임라인이 의미를 갖도록.
    congestion = {"시청": 90, "강남": 160, "역삼": 140, "선릉": 120,
                  "삼성": 90, "종합운동장": 60, "잠실": 30, "사당": 20}
    for name, pct in congestion.items():
        for slot in ("08:00", "08:30", "09:00"):
            for direction in ("상선", "하선"):
                con.execute(
                    "INSERT INTO congestion_stat VALUES (?,?,?,?,?,?,?)",
                    ["5호선", name, "평일", direction, slot, float(pct), "official"],
                )
    con.execute(
        "INSERT INTO station_master (station_key, name, name_norm, line, seq, branch_no,"
        " lat, lng) VALUES ('9호선|노량진','노량진','노량진','9호선',1,0,37.51,126.94)"
    )


@pytest.fixture
def make_client(tmp_path):
    created: list[AppState] = []

    def _make(realtime=None):
        con = duckdb.connect(":memory:")
        init_schema(con)
        seed(con)
        settings = Settings(
            api_key="test", realtime_api_key=None, db_path=tmp_path / "x.duckdb",
            raw_dir=tmp_path, snapshot_dir=tmp_path, realtime_cache_ttl_sec=30,
            similar_threshold_pct=8.0,
        )
        state = AppState(settings=settings, con=con, realtime=realtime or FakeRealtime())
        created.append(state)
        client = TestClient(app)
        client.app.dependency_overrides = {}
        # lifespan 이 만드는 실제 상태 대신 테스트 상태를 쓴다.
        app.state.app_state = state
        return client

    yield _make
    for state in created:
        state.con.close()


class TestHealthAndLines:
    def test_health_reports_data_readiness(self, make_client):
        client = make_client()
        body = client.get("/api/health").json()
        assert body["status"] == "ok"
        assert body["dataReady"] is True
        assert body["stations"] == len(LINE_5) + 1
        assert body["congestionSource"] == "official"

    def test_health_flags_missing_realtime_key(self, make_client):
        assert make_client().get("/api/health").json()["realtimeEnabled"] is False

    def test_lines_marks_prediction_support(self, make_client):
        lines = {row["line"]: row for row in make_client().get("/api/lines").json()["lines"]}
        assert lines["5호선"]["predictionAvailable"] is True
        assert lines["9호선"]["predictionAvailable"] is False

    def test_lines_include_bounds_for_map_fitting(self, make_client):
        bounds = make_client().get("/api/lines").json()["lines"][0]["bounds"]
        assert set(bounds) == {"south", "north", "west", "east"}
        assert bounds["south"] <= bounds["north"]


class TestStations:
    def test_all_stations(self, make_client):
        body = make_client().get("/api/stations").json()
        assert body["count"] == len(LINE_5) + 1

    def test_filtered_by_line_and_ordered_by_seq(self, make_client):
        body = make_client().get("/api/stations", params={"line": "5호선"}).json()
        assert [s["name"] for s in body["stations"]] == LINE_5

    def test_line_name_is_normalized(self, make_client):
        body = make_client().get("/api/stations", params={"line": "05호선"}).json()
        assert body["count"] == len(LINE_5)

    def test_station_payload_has_map_fields(self, make_client):
        station = make_client().get(
            "/api/stations", params={"line": "5호선"}
        ).json()["stations"][0]
        assert {"lat", "lng", "seq", "branchNo", "transfer", "line"} <= set(station)

    def test_unknown_line_returns_empty_not_error(self, make_client):
        body = make_client().get("/api/stations", params={"line": "99호선"}).json()
        assert body["count"] == 0


class TestHeatmap:
    def test_grid_shape_matches_slots(self, make_client):
        body = make_client().get("/api/heatmap", params={"line": "5호선"}).json()
        assert len(body["slots"]) == 20
        for station in body["stations"]:
            assert len(station["values"]) == len(body["slots"])

    def test_values_present_for_seeded_slots(self, make_client):
        body = make_client().get("/api/heatmap", params={"line": "5호선"}).json()
        index = body["slots"].index("08:00")
        gangnam = next(s for s in body["stations"] if s["name"] == "강남")
        assert gangnam["values"][index] == 160.0

    def test_source_is_disclosed(self, make_client):
        body = make_client().get("/api/heatmap", params={"line": "5호선"}).json()
        assert body["source"] == "official"

    def test_official_is_never_diluted_by_estimated(self, make_client):
        # 두 소스를 함께 평균하면 공식 통계가 추정 오차만큼 오염되는데,
        # 결과가 그냥 숫자 하나라 틀렸다는 걸 알아챌 수 없다.
        client = make_client()
        con = app.state.app_state.con
        con.execute(
            "INSERT INTO congestion_stat VALUES"
            " ('5호선','강남','평일','상선','08:00', 999.0, 'estimated')"
        )
        body = client.get("/api/heatmap", params={"line": "5호선"}).json()
        index = body["slots"].index("08:00")
        gangnam = next(s for s in body["stations"] if s["name"] == "강남")
        assert gangnam["values"][index] == 160.0   # 공식값 그대로
        assert gangnam["source"] == "official"

    def test_station_without_official_falls_back_to_estimated(self, make_client):
        # 공식 통계는 서울교통공사 구간만 다룬다. 같은 노선이라도 추정치뿐인 역이 있다.
        client = make_client()
        con = app.state.app_state.con
        con.execute(
            "INSERT INTO station_master (station_key, name, name_norm, line, seq,"
            " branch_no, lat, lng) VALUES ('5호선|연천','연천','연천','5호선',99,0,38.0,127.0)"
        )
        con.execute(
            "INSERT INTO congestion_stat VALUES"
            " ('5호선','연천','평일','상선','08:00', 3.0, 'estimated')"
        )
        body = client.get("/api/heatmap", params={"line": "5호선"}).json()
        yeoncheon = next(s for s in body["stations"] if s["name"] == "연천")
        assert yeoncheon["source"] == "estimated"
        assert yeoncheon["values"][body["slots"].index("08:00")] == 3.0
        # 노선 대표 소스는 여전히 공식이어야 한다(대다수 역이 공식이므로).
        assert body["source"] == "official"

    def test_per_station_source_is_exposed(self, make_client):
        body = make_client().get("/api/heatmap", params={"line": "5호선"}).json()
        assert all("source" in s for s in body["stations"])

    def test_stations_ordered_by_seq(self, make_client):
        body = make_client().get("/api/heatmap", params={"line": "5호선"}).json()
        seqs = [s["seq"] for s in body["stations"]]
        assert seqs == sorted(seqs)

    def test_line_without_data_returns_404(self, make_client):
        assert make_client().get("/api/heatmap", params={"line": "9호선"}).status_code == 404


class TestRealtimePositions:
    def test_positions_are_geolocated_from_master(self, make_client):
        client = make_client(FakeRealtime(positions=[position("2234", "강남")]))
        body = client.get("/api/realtime/positions", params={"line": "5호선"}).json()
        assert body["count"] == 1
        train = body["trains"][0]
        assert train["lat"] is not None and train["lng"] is not None
        assert train["seq"] == LINE_5.index("강남") + 1

    def test_unmatched_station_has_null_coordinates(self, make_client):
        client = make_client(FakeRealtime(positions=[position("9999", "어딘가")]))
        train = client.get(
            "/api/realtime/positions", params={"line": "5호선"}
        ).json()["trains"][0]
        assert train["lat"] is None

    def test_source_is_disclosed(self, make_client):
        client = make_client(FakeRealtime(positions=[], source="replay"))
        body = client.get("/api/realtime/positions", params={"line": "5호선"}).json()
        assert body["source"] == "replay"

    def test_empty_feed_is_not_an_error(self, make_client):
        response = make_client().get("/api/realtime/positions", params={"line": "5호선"})
        assert response.status_code == 200
        assert response.json()["trains"] == []


class TestPredictStation:
    def _client(self, make_client, arrivals, source="live"):
        return make_client(FakeRealtime(arrivals=arrivals, source=source))

    def test_this_and_next_are_predicted(self, make_client):
        client = self._client(
            make_client, [arrival("2234", 120), arrival("2236", 480)]
        )
        body = client.get(
            "/api/predict/station/강남", params={"line": "5호선", "direction": "상선", "at": AT}
        ).json()
        assert body["thisTrain"]["trainNo"] == "2234"
        assert body["nextTrain"]["trainNo"] == "2236"
        assert body["thisTrain"]["baselinePct"] == 160.0

    def test_trains_are_ordered_by_eta(self, make_client):
        client = self._client(
            make_client, [arrival("LATE", 600), arrival("SOON", 60)]
        )
        body = client.get(
            "/api/predict/station/강남", params={"line": "5호선", "direction": "상선", "at": AT}
        ).json()
        assert body["thisTrain"]["trainNo"] == "SOON"

    def test_headway_widens_expected_congestion(self, make_client):
        # 두 번째 열차는 앞 열차와 간격이 벌어져 기준보다 붐벼야 한다.
        client = self._client(
            make_client, [arrival("A", 60), arrival("B", 900)]
        )
        body = client.get(
            "/api/predict/station/강남", params={"line": "5호선", "direction": "상선", "at": AT}
        ).json()
        assert body["nextTrain"]["headway"]["available"] is True
        assert body["nextTrain"]["expectedPct"] > body["nextTrain"]["baselinePct"]

    def test_recommendation_is_present_with_a_verdict(self, make_client):
        client = self._client(
            make_client, [arrival("A", 60), arrival("B", 900)]
        )
        body = client.get(
            "/api/predict/station/강남", params={"line": "5호선", "direction": "상선", "at": AT}
        ).json()
        assert body["recommendation"]["verdict"] in {"take_this", "take_next", "similar"}
        assert body["recommendation"]["message"]

    def test_reasons_are_returned_for_the_ui(self, make_client):
        client = self._client(make_client, [arrival("A", 60), arrival("B", 900)])
        body = client.get(
            "/api/predict/station/강남", params={"line": "5호선", "direction": "상선", "at": AT}
        ).json()
        assert isinstance(body["thisTrain"]["reasons"], list)

    def test_opposite_direction_trains_are_never_compared(self, make_client):
        # 방향을 안 줬을 때 반대 방향 열차를 "다음 열차"로 추천하면 안 된다.
        # 그 열차는 사용자가 가려는 곳의 반대로 간다.
        client = self._client(
            make_client,
            [arrival("UP", 120, direction="상선"), arrival("DOWN", 180, direction="하선")],
        )
        body = client.get(
            "/api/predict/station/강남", params={"line": "5호선", "at": AT}
        ).json()
        assert body["thisTrain"]["trainNo"] == "UP"
        assert body["nextTrain"] is None
        assert body["direction"] == "상선"
        assert body["directionInferred"] is True
        assert body["arrivalCount"] == 1

    def test_inferred_direction_follows_the_soonest_train(self, make_client):
        client = self._client(
            make_client,
            [arrival("DOWN", 60, direction="하선"), arrival("UP", 200, direction="상선")],
        )
        body = client.get(
            "/api/predict/station/강남", params={"line": "5호선", "at": AT}
        ).json()
        assert body["direction"] == "하선"
        assert body["thisTrain"]["trainNo"] == "DOWN"

    def test_explicit_direction_is_not_marked_inferred(self, make_client):
        client = self._client(make_client, [arrival("UP", 120, direction="상선")])
        body = client.get(
            "/api/predict/station/강남",
            params={"line": "5호선", "direction": "상선", "at": AT},
        ).json()
        assert body["directionInferred"] is False

    def test_realtime_direction_wording_is_accepted(self, make_client):
        # 실시간 원본 어휘('상행')로 물어봐도 통계 어휘('상선')와 같이 취급돼야 한다.
        client = self._client(make_client, [arrival("UP", 120, direction="상선")])
        body = client.get(
            "/api/predict/station/강남",
            params={"line": "5호선", "direction": "상행", "at": AT},
        ).json()
        assert body["arrivalCount"] == 1
        assert body["direction"] == "상선"

    def test_direction_filter_excludes_other_direction(self, make_client):
        client = self._client(
            make_client,
            [arrival("UP", 120, direction="상선"), arrival("DOWN", 60, direction="하선")],
        )
        body = client.get(
            "/api/predict/station/강남", params={"line": "5호선", "direction": "상선", "at": AT}
        ).json()
        assert body["arrivalCount"] == 1
        assert body["thisTrain"]["trainNo"] == "UP"

    def test_other_line_arrivals_are_excluded(self, make_client):
        client = self._client(
            make_client, [arrival("OTHER", 60, line="9호선"), arrival("MINE", 120)]
        )
        body = client.get("/api/predict/station/강남", params={"line": "5호선", "at": AT}).json()
        assert body["thisTrain"]["trainNo"] == "MINE"

    def test_no_trains_yields_reason_not_error(self, make_client):
        response = make_client().get("/api/predict/station/강남", params={"line": "5호선", "at": AT})
        assert response.status_code == 200
        body = response.json()
        assert body["thisTrain"] is None
        assert "열차" in body["reason"]

    def test_unpredictable_line_returns_arrivals_only(self, make_client):
        client = self._client(
            make_client, [arrival("9001", 90, line="9호선", station="노량진")]
        )
        body = client.get("/api/predict/station/노량진", params={"line": "9호선"}).json()
        assert body["predictionAvailable"] is False
        assert "thisTrain" not in body
        assert body["arrivals"][0]["trainNo"] == "9001"
        assert "혼잡도 통계가 없어" in body["reason"]

    def test_unknown_station_returns_404(self, make_client):
        response = make_client().get(
            "/api/predict/station/없는역", params={"line": "5호선"}
        )
        assert response.status_code == 404

    def test_source_is_disclosed(self, make_client):
        client = self._client(make_client, [arrival("A", 60)], source="replay")
        body = client.get("/api/predict/station/강남", params={"line": "5호선", "at": AT}).json()
        assert body["source"] == "replay"


class TestSeatTimeline:
    def test_timeline_returned_when_destination_given(self, make_client):
        client = make_client(FakeRealtime(arrivals=[arrival("A", 60)]))
        body = client.get(
            "/api/predict/station/강남",
            params={"line": "5호선", "direction": "상선", "dest": "사당", "at": AT},
        ).json()
        stops = body["timeline"]["stops"]
        assert [s["name"] for s in stops] == LINE_5[LINE_5.index("강남"):]

    def test_seat_marker_points_at_first_uncrowded_stop(self, make_client):
        client = make_client(FakeRealtime(arrivals=[arrival("A", 60)]))
        timeline = client.get(
            "/api/predict/station/강남",
            params={"line": "5호선", "direction": "상선", "dest": "사당", "at": AT},
        ).json()["timeline"]
        assert timeline["seatFrom"] == "잠실"
        assert timeline["seatAfterMinutes"] is not None
        assert timeline["stops"][timeline["seatFromIndex"]]["seatLikely"] is True

    def test_congestion_falls_along_the_route(self, make_client):
        client = make_client(FakeRealtime(arrivals=[arrival("A", 60)]))
        stops = client.get(
            "/api/predict/station/강남",
            params={"line": "5호선", "direction": "상선", "dest": "사당", "at": AT},
        ).json()["timeline"]["stops"]
        assert stops[0]["congestionPct"] > stops[-1]["congestionPct"]

    def test_no_timeline_key_without_destination(self, make_client):
        client = make_client(FakeRealtime(arrivals=[arrival("A", 60)]))
        body = client.get("/api/predict/station/강남", params={"line": "5호선", "at": AT}).json()
        assert "timeline" not in body

    def test_unreachable_destination_explains_itself(self, make_client):
        client = make_client(FakeRealtime(arrivals=[arrival("A", 60)]))
        timeline = client.get(
            "/api/predict/station/강남",
            params={"line": "5호선", "dest": "노량진", "at": AT},
        ).json()["timeline"]
        assert timeline["stops"] == []
        assert "찾지 못했습니다" in timeline["reason"]


class TestFrontendServing:
    def test_root_serves_the_dashboard(self, make_client):
        response = make_client().get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
