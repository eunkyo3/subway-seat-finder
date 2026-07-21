"""혼잡도 로더 테스트 — 공식 파일 경로와 추정 경로 양쪽."""

import duckdb
import pandas as pd
import pytest

from backend.app.db import init_schema
from backend.app.etl.load_congestion import (
    CALIBRATION_TARGET_PCT,
    MAX_ESTIMATED_PCT,
    SOURCE_ESTIMATED,
    SOURCE_OFFICIAL,
    balance_flows,
    directional_profile,
    estimate_congestion,
    find_congestion_files,
    load_congestion,
    load_official_congestion,
    parse_congestion_frame,
    parse_time_column,
)


@pytest.fixture
def con():
    connection = duckdb.connect(":memory:")
    init_schema(connection)
    yield connection
    connection.close()


def seed_line(con, line="2호선", stations=(("A", 900, 0), ("B", 300, 300), ("C", 0, 900))):
    """seq 순서대로 역과 승하차를 심는다. stations 는 (역명, 승차, 하차)."""
    for seq, (name, board, alight) in enumerate(stations, start=1):
        con.execute(
            "INSERT INTO station_master (station_key, name, name_norm, line, seq,"
            " branch_no, lat, lng) VALUES (?,?,?,?,?,0,37.5,127.0)",
            [f"{line}|{name}", name, name, line, seq],
        )
        con.execute(
            "INSERT INTO station_flow VALUES (?,?,?,?,?,?)",
            [line, name, "202606", 8, float(board), float(alight)],
        )


class TestParseTimeColumn:
    def test_korean_hour_minute(self):
        assert parse_time_column("5시30분") == "05:30"
        assert parse_time_column("18시00분") == "18:00"

    def test_colon_form(self):
        assert parse_time_column("05:30") == "05:30"
        assert parse_time_column("5:30") == "05:30"

    def test_hour_only(self):
        assert parse_time_column("8시") == "08:00"

    def test_midnight_wraps_to_zero(self):
        assert parse_time_column("24시00분") == "00:00"

    def test_range_form_uses_the_start_of_the_slot(self):
        # 2026년 분기 파일은 '05:30~06:00' 구간 표기를 쓴다. 값은 그 구간의 통계다.
        assert parse_time_column("05:30~06:00") == "05:30"
        assert parse_time_column("08:00~08:30") == "08:00"
        assert parse_time_column("23:30~24:00") == "23:30"

    def test_range_form_tolerates_spacing_and_dashes(self):
        assert parse_time_column("05:30 ~ 06:00") == "05:30"
        assert parse_time_column("05:30-06:00") == "05:30"
        assert parse_time_column("5시30분~6시00분") == "05:30"

    def test_non_time_columns_return_none(self):
        for label in ("호선", "출발역", "상하구분", "연번", "역번호", ""):
            assert parse_time_column(label) is None

    def test_out_of_range_returns_none(self):
        assert parse_time_column("25시00분") is None
        assert parse_time_column("5시99분") is None


class TestParseCongestionFrame:
    def test_standard_layout(self):
        frame = pd.DataFrame(
            [
                {"요일구분": "평일", "호선": "2호선", "출발역": "강남",
                 "상하구분": "상선", "8시00분": 152.3, "8시30분": 148.0},
            ]
        )
        rows = parse_congestion_frame(frame)
        assert rows == [
            ("2호선", "강남", "평일", "상선", "08:00", 152.3, SOURCE_OFFICIAL),
            ("2호선", "강남", "평일", "상선", "08:30", 148.0, SOURCE_OFFICIAL),
        ]

    def test_numeric_line_column_from_excel(self):
        # 같은 데이터셋인데 어떤 분기 파일은 호선을 정수 1 로 저장한다.
        frame = pd.DataFrame(
            [{"요일구분": "평일", "호선": 1, "출발역": "청량리",
              "상하구분": "상선", "5시30분": 7.2}]
        )
        assert parse_congestion_frame(frame)[0][:2] == ("1호선", "청량리")

    def test_2026_layout_with_range_columns_and_renamed_headers(self):
        # 2026년 파일: 요일구분->구분, 출발역->역명, 시간대는 구간 표기.
        frame = pd.DataFrame(
            [{"구분": "평일", "호선": "1호선", "역번호": 150, "역명": "서울역",
              "상하구분": "상선", "05:30~06:00": 8.03, "06:00~06:30": 20.67}]
        )
        rows = parse_congestion_frame(frame)
        assert [(r[0], r[1], r[2], r[3], r[4]) for r in rows] == [
            ("1호선", "서울", "평일", "상선", "05:30"),
            ("1호선", "서울", "평일", "상선", "06:00"),
        ]

    def test_station_column_is_not_confused_with_station_number(self):
        # '역번호' 가 '역' 부분일치로 잡히면 역명 자리에 숫자가 들어간다.
        frame = pd.DataFrame(
            [{"구분": "평일", "호선": "1호선", "역번호": 150, "역명": "서울역",
              "상하구분": "상선", "05:30~06:00": 8.0}]
        )
        assert parse_congestion_frame(frame)[0][1] == "서울"

    def test_day_type_column_is_not_confused_with_direction_column(self):
        # '구분' 부분일치가 '상하구분' 을 먼저 잡으면 요일이 방향값으로 오염된다.
        frame = pd.DataFrame(
            [{"구분": "토요일", "호선": "2호선", "역명": "강남",
              "상하구분": "하선", "08:00~08:30": 100.0}]
        )
        row = parse_congestion_frame(frame)[0]
        assert row[2] == "토요일"
        assert row[3] == "하선"

    def test_alternate_column_names_are_detected(self):
        frame = pd.DataFrame(
            [{"요일": "토요일", "노선": "07호선", "역명": "건대입구",
              "방향": "하선", "18:00": 90.0}]
        )
        rows = parse_congestion_frame(frame)
        assert rows[0][:5] == ("7호선", "건대입구", "토요일", "하선", "18:00")

    def test_day_type_variants_are_normalized(self):
        for raw, expected in [("평일", "평일"), ("토요일", "토요일"),
                              ("일요일", "일요일"), ("공휴일", "일요일")]:
            frame = pd.DataFrame([{"요일구분": raw, "호선": "2호선",
                                   "출발역": "강남", "8시00분": 100.0}])
            assert parse_congestion_frame(frame)[0][2] == expected

    def test_direction_variants_are_normalized(self):
        for raw, expected in [("상선", "상선"), ("내선", "상선"),
                              ("하선", "하선"), ("외선", "하선")]:
            frame = pd.DataFrame([{"호선": "2호선", "출발역": "강남",
                                   "상하구분": raw, "8시00분": 100.0}])
            assert parse_congestion_frame(frame)[0][3] == expected

    def test_missing_optional_columns_use_defaults(self):
        frame = pd.DataFrame([{"호선": "2호선", "출발역": "강남", "8시00분": 100.0}])
        row = parse_congestion_frame(frame)[0]
        assert row[2] == "평일" and row[3] == "전체"

    def test_non_numeric_and_blank_cells_are_skipped(self):
        frame = pd.DataFrame(
            [{"호선": "2호선", "출발역": "강남", "8시00분": "-", "8시30분": None,
              "9시00분": 120.0}]
        )
        rows = parse_congestion_frame(frame)
        assert [r[4] for r in rows] == ["09:00"]

    def test_frame_without_time_columns_raises(self):
        with pytest.raises(ValueError, match="시간대 컬럼"):
            parse_congestion_frame(pd.DataFrame([{"호선": "2호선", "출발역": "강남"}]))

    def test_frame_without_station_column_raises(self):
        with pytest.raises(ValueError, match="호선/역명"):
            parse_congestion_frame(pd.DataFrame([{"연번": 1, "8시00분": 100.0}]))


class TestDirectionalProfile:
    def test_value_is_the_load_arriving_at_each_station(self):
        profile = directional_profile([("A", 900, 0), ("B", 300, 300), ("C", 0, 900)], False)
        # 기점에 들어오는 열차는 비어 있고, 절반씩 흐른다고 보므로 A 에서 450 이 탄다.
        assert profile["A"] == pytest.approx(0.0)
        assert profile["B"] == pytest.approx(450.0)
        assert profile["C"] == pytest.approx(450.0)

    def test_reverse_direction_of_one_way_flow_stays_empty(self):
        # A 에서만 타고 C 에서만 내리는 데이터라면, C->A 방향 열차는 비어 있어야 한다.
        profile = directional_profile([("A", 900, 0), ("B", 300, 300), ("C", 0, 900)], True)
        assert set(profile.values()) == {0.0}

    def test_never_negative(self):
        profile = directional_profile([("A", 0, 1000), ("B", 0, 1000)], False)
        assert min(profile.values()) >= 0

    def test_peak_sits_where_the_line_is_fullest(self):
        stations = [("A", 1000, 0), ("B", 800, 100), ("C", 100, 800), ("D", 0, 1000)]
        profile = directional_profile(stations, False)
        assert profile["C"] == max(profile.values())
        assert profile["A"] == 0.0

    def test_empty_input(self):
        assert directional_profile([], False) == {}

    def test_loop_line_is_lifted_instead_of_clamped(self):
        # 순환선은 기점이 없다. 어디서 시작해도 가장 비는 지점이 0 이어야 한다.
        stations = [("A", 0, 600), ("B", 1000, 0), ("C", 200, 600)]
        profile = directional_profile(stations, False, loop=True)
        assert min(profile.values()) == pytest.approx(0.0)
        # 순환선을 선형처럼 자르면 A 에서의 하차가 통째로 버려진다.
        linear = directional_profile(stations, False, loop=False)
        assert profile != linear

    def test_loop_profile_has_no_artificial_zero_at_the_start(self):
        stations = [("A", 100, 500), ("B", 800, 100), ("C", 300, 600)]
        profile = directional_profile(stations, False, loop=True)
        assert profile["A"] > 0


class TestBalanceFlows:
    def test_alighting_is_scaled_to_match_boarding(self):
        balanced = balance_flows([("A", 600, 100), ("B", 400, 100)])
        assert sum(a for _, _, a in balanced) == pytest.approx(1000.0)
        assert sum(b for _, b, _ in balanced) == pytest.approx(1000.0)

    def test_relative_shape_of_alighting_is_preserved(self):
        balanced = balance_flows([("A", 500, 100), ("B", 500, 300)])
        alights = {n: a for n, _, a in balanced}
        assert alights["B"] / alights["A"] == pytest.approx(3.0)

    def test_boarding_is_never_modified(self):
        stations = [("A", 600, 100), ("B", 400, 900)]
        assert [b for _, b, _ in balance_flows(stations)] == [600, 400]

    def test_degenerate_hours_pass_through(self):
        # 심야처럼 승차나 하차가 0 인 시간대는 보정할 근거가 없다.
        assert balance_flows([("A", 0, 0)]) == [("A", 0, 0)]
        assert balance_flows([("A", 100, 0)]) == [("A", 100, 0)]
        assert balance_flows([]) == []


class TestEstimateCongestion:
    def test_writes_both_directions_and_all_slots(self, con):
        seed_line(con)
        assert estimate_congestion(con) > 0
        directions = {
            r[0] for r in con.execute("SELECT DISTINCT direction FROM congestion_stat").fetchall()
        }
        assert directions == {"상선", "하선"}
        slots = {
            r[0] for r in con.execute("SELECT DISTINCT time_slot FROM congestion_stat").fetchall()
        }
        assert slots == {"08:00", "08:30"}

    def test_source_is_marked_estimated(self, con):
        seed_line(con)
        estimate_congestion(con)
        sources = {
            r[0] for r in con.execute("SELECT DISTINCT source FROM congestion_stat").fetchall()
        }
        assert sources == {SOURCE_ESTIMATED}

    def test_calibration_puts_peak_at_target(self, con):
        seed_line(con)
        estimate_congestion(con)
        peak = con.execute("SELECT max(congestion_pct) FROM congestion_stat").fetchone()[0]
        assert peak == pytest.approx(CALIBRATION_TARGET_PCT, rel=0.01)

    def test_values_never_exceed_cap_or_go_negative(self, con):
        seed_line(con, stations=(("A", 10**7, 0), ("B", 1, 1), ("C", 0, 10**7)))
        estimate_congestion(con)
        lo, hi = con.execute(
            "SELECT min(congestion_pct), max(congestion_pct) FROM congestion_stat"
        ).fetchone()
        assert lo >= 0
        assert hi <= MAX_ESTIMATED_PCT

    def test_branch_walks_separately_without_distorting_the_main_line(self, con):
        def main_line_shape():
            rows = con.execute(
                "SELECT name_norm, congestion_pct FROM congestion_stat"
                " WHERE direction='상선' AND name_norm IN ('A','B','C') ORDER BY name_norm"
            ).fetchall()
            peak = max(pct for _, pct in rows) or 1.0
            return {name: round(pct / peak, 6) for name, pct in rows}

        seed_line(con)
        estimate_congestion(con)
        before = main_line_shape()

        # 지선을 붙인다. 전역 보정 때문에 절대값은 움직일 수 있지만,
        # 본선 안에서의 상대 형상은 지선에 영향받지 않아야 한다.
        for seq, name in enumerate(["Y", "Z"], start=90):
            con.execute(
                "INSERT INTO station_master (station_key, name, name_norm, line, seq,"
                " branch_no, lat, lng) VALUES (?,?,?,'2호선',?,1,37.5,127.0)",
                [f"2호선|{name}", name, name, seq],
            )
        con.execute("INSERT INTO station_flow VALUES ('2호선','Y','202606',8,1000,0)")
        con.execute("INSERT INTO station_flow VALUES ('2호선','Z','202606',8,0,1000)")

        estimate_congestion(con)
        assert main_line_shape() == before
        # 지선 역도 자기 구간 기준으로 값을 받는다.
        assert con.execute(
            "SELECT count(*) FROM congestion_stat WHERE name_norm IN ('Y','Z')"
        ).fetchone()[0] > 0

    def test_empty_flow_returns_zero(self, con):
        assert estimate_congestion(con) == 0

    def test_rerun_replaces_instead_of_duplicating(self, con):
        seed_line(con)
        first = estimate_congestion(con)
        second = estimate_congestion(con)
        assert first == second
        assert con.execute("SELECT count(*) FROM congestion_stat").fetchone()[0] == first


class TestLoadOfficialCongestion:
    def test_missing_directory_returns_zero(self, con, tmp_path):
        assert load_official_congestion(con, tmp_path / "nope") == 0

    def test_empty_directory_returns_zero(self, con, tmp_path):
        assert load_official_congestion(con, tmp_path) == 0

    def test_csv_file_is_loaded(self, con, tmp_path):
        path = tmp_path / "congestion.csv"
        pd.DataFrame(
            [{"요일구분": "평일", "호선": "2호선", "출발역": "강남",
              "상하구분": "상선", "8시00분": 155.0}]
        ).to_csv(path, index=False, encoding="utf-8-sig")

        assert load_official_congestion(con, tmp_path) == 1
        row = con.execute(
            "SELECT line, name_norm, congestion_pct, source FROM congestion_stat"
        ).fetchone()
        assert row == ("2호선", "강남", 155.0, SOURCE_OFFICIAL)

    def test_excel_file_is_loaded(self, con, tmp_path):
        path = tmp_path / "congestion.xlsx"
        pd.DataFrame(
            [{"요일구분": "평일", "호선": "4호선", "출발역": "사당",
              "상하구분": "하선", "18시30분": 143.0}]
        ).to_excel(path, index=False)
        assert load_official_congestion(con, tmp_path) == 1

    def test_unparseable_file_is_skipped_without_raising(self, con, tmp_path):
        (tmp_path / "junk.csv").write_text("a,b,c\n1,2,3\n", encoding="utf-8")
        assert load_official_congestion(con, tmp_path) == 0

    def test_excel_lock_files_are_ignored(self, tmp_path):
        (tmp_path / "~$open.xlsx").write_text("", encoding="utf-8")
        (tmp_path / "real.csv").write_text("x\n", encoding="utf-8")
        assert [p.name for p in find_congestion_files(tmp_path)] == ["real.csv"]


class TestLoadCongestion:
    def test_official_and_estimated_coexist_with_official_present(self, con, tmp_path):
        seed_line(con)
        pd.DataFrame(
            [{"요일구분": "평일", "호선": "2호선", "출발역": "A",
              "상하구분": "상선", "8시00분": 155.0}]
        ).to_csv(tmp_path / "c.csv", index=False, encoding="utf-8-sig")

        counts = load_congestion(con, tmp_path)
        assert counts[SOURCE_OFFICIAL] == 1
        assert counts[SOURCE_ESTIMATED] > 0
        # 같은 역·시간대에 두 소스가 공존해야 조회 시 우선순위를 고를 수 있다.
        both = con.execute(
            "SELECT source FROM congestion_stat WHERE name_norm='A' AND time_slot='08:00'"
            " AND direction='상선'"
        ).fetchall()
        assert {r[0] for r in both} == {SOURCE_OFFICIAL, SOURCE_ESTIMATED}

    def test_estimated_only_when_no_file(self, con, tmp_path):
        seed_line(con)
        counts = load_congestion(con, tmp_path)
        assert counts[SOURCE_OFFICIAL] == 0
        assert counts[SOURCE_ESTIMATED] > 0
