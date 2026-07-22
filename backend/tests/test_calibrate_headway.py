"""배차간격 캘리브레이션 테스트.

핵심은 그룹 경계다. 다른 역·다른 방향·다른 수집 시각의 열차를 이웃으로
붙이면 실측이 아니라 가짜 간격이 섞인다 — 그 값이 상수 제안의 근거가 되므로
경계 하나하나를 못박는다.
"""

from __future__ import annotations

import json
from datetime import datetime

import duckdb
import pytest

from backend.app.config import Settings
from backend.app.db import init_schema
from backend.app.etl import calibrate_headway
from backend.app.etl.calibrate_headway import extract_headways, summarize

# 2026-07-22 은 수요일(평일), 2026-07-25 은 토요일.
WEEKDAY = datetime(2026, 7, 22, 8, 0, 0)
SATURDAY = datetime(2026, 7, 25, 8, 0, 0)


@pytest.fixture()
def con():
    connection = duckdb.connect(":memory:")
    init_schema(connection)
    yield connection
    connection.close()


def add_arrival(
    con,
    *,
    train_no: str,
    eta_sec: int,
    collected_at: datetime = WEEKDAY,
    subway_id: str = "1002",
    station: str = "강남",
    direction: str = "상선",
) -> None:
    con.execute(
        "INSERT INTO arrival_log (subway_id, station_id, station_name, train_no,"
        " arrival_eta_sec, express_yn, terminal_station, direction, collected_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [subway_id, "1002000230", station, train_no, eta_sec, False, "성수", direction, collected_at],
    )


class TestExtractHeadways:
    def test_consecutive_etas_become_one_gap(self, con):
        add_arrival(con, train_no="2101", eta_sec=60)
        add_arrival(con, train_no="2103", eta_sec=360)

        samples = extract_headways(con, "평일")

        assert samples == [("2호선", 8, 300.0)]

    def test_different_stations_are_not_paired(self, con):
        add_arrival(con, train_no="2101", eta_sec=60, station="강남")
        add_arrival(con, train_no="2103", eta_sec=360, station="역삼")

        assert extract_headways(con, "평일") == []

    def test_different_directions_are_not_paired(self, con):
        add_arrival(con, train_no="2101", eta_sec=60, direction="상선")
        add_arrival(con, train_no="2103", eta_sec=360, direction="하선")

        assert extract_headways(con, "평일") == []

    def test_different_collection_rounds_are_not_paired(self, con):
        add_arrival(con, train_no="2101", eta_sec=60, collected_at=WEEKDAY)
        add_arrival(
            con, train_no="2103", eta_sec=360,
            collected_at=WEEKDAY.replace(minute=5),
        )

        assert extract_headways(con, "평일") == []

    def test_same_train_repeated_is_not_a_gap(self, con):
        # 같은 열차가 같은 그룹에 두 번 잡히는 건 배차가 아니라 중복 관측이다.
        add_arrival(con, train_no="2101", eta_sec=60)
        add_arrival(con, train_no="2101", eta_sec=90)

        assert extract_headways(con, "평일") == []

    def test_absurd_gap_is_discarded(self, con):
        # 1시간 넘는 간격은 배차가 아니라 운행 공백(막차 전후)이다.
        add_arrival(con, train_no="2101", eta_sec=60)
        add_arrival(con, train_no="2103", eta_sec=60 + 3700)

        assert extract_headways(con, "평일") == []

    def test_hour_bucketed_by_arrival_time_not_collection_time(self, con):
        # 07:58 수집이라도 08:02 도착이면 8시대 배차다.
        collected = WEEKDAY.replace(hour=7, minute=58)
        add_arrival(con, train_no="2101", eta_sec=30, collected_at=collected)
        add_arrival(con, train_no="2103", eta_sec=240, collected_at=collected)

        samples = extract_headways(con, "평일")

        assert samples == [("2호선", 8, 210.0)]

    def test_day_type_filters_by_weekday(self, con):
        add_arrival(con, train_no="2101", eta_sec=60, collected_at=SATURDAY)
        add_arrival(con, train_no="2103", eta_sec=360, collected_at=SATURDAY)

        assert extract_headways(con, "평일") == []
        assert extract_headways(con, "토요일") == [("2호선", 8, 300.0)]


class TestSummarize:
    def test_deviation_against_nominal(self):
        # 8시 기준 배차는 2.5분. 실측 중앙값이 150초(2.5분)면 편차 0이다.
        samples = [("2호선", 8, 150.0)] * 30

        (cell,) = summarize(samples)

        assert cell["n"] == 30
        assert cell["observedMedianMin"] == 2.5
        assert cell["nominalMin"] == 2.5
        assert cell["deviationPct"] == 0.0
        assert cell["sufficient"] is True

    def test_small_cell_marked_insufficient(self):
        samples = [("2호선", 8, 150.0)] * 29

        (cell,) = summarize(samples)

        assert cell["sufficient"] is False


class TestMain:
    @pytest.fixture()
    def settings(self, tmp_path, monkeypatch):
        s = Settings(
            api_key=None,
            realtime_api_key=None,
            db_path=tmp_path / "subway.duckdb",
            raw_dir=tmp_path / "raw",
            snapshot_dir=tmp_path / "snapshots",
            realtime_cache_ttl_sec=30,
            similar_threshold_pct=8.0,
        )
        monkeypatch.setattr(calibrate_headway, "load_settings", lambda: s)
        return s

    def test_no_samples_exits_nonzero(self, settings):
        duckdb_con = duckdb.connect(str(settings.db_path))
        init_schema(duckdb_con)
        duckdb_con.close()

        assert calibrate_headway.main([]) == 1

    def test_reports_and_writes_json(self, settings, tmp_path):
        con = duckdb.connect(str(settings.db_path))
        init_schema(con)
        add_arrival(con, train_no="2101", eta_sec=60)
        add_arrival(con, train_no="2103", eta_sec=360)
        con.close()

        out = tmp_path / "calibration.json"
        rc = calibrate_headway.main(["--min-samples", "1", "--json", str(out)])

        assert rc == 0
        document = json.loads(out.read_text(encoding="utf-8"))
        assert document["dayType"] == "평일"
        assert document["cells"] == [
            {
                "line": "2호선",
                "hour": 8,
                "n": 1,
                "observedMedianMin": 5.0,
                "nominalMin": 2.5,
                "deviationPct": 100.0,
                "sufficient": True,
            }
        ]
