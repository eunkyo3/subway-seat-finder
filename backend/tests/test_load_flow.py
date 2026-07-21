"""승하차 ETL 테스트 (CardSubwayTime)."""

from backend.app.etl.load_flow import aggregate_flow_rows, parse_flow_row


def wide_row(line="2호선", station="강남", ym="202606", **hours):
    """HR_x_GET_ON/OFF 와이드 행을 만든다. hours 는 {4: (승차, 하차)} 형태."""
    row = {"USE_MM": ym, "SBWY_ROUT_LN_NM": line, "STTN": station, "JOB_YMD": "20260701"}
    for hour, (on, off) in hours.items():
        row[f"HR_{hour}_GET_ON_NOPE"] = on
        row[f"HR_{hour}_GET_OFF_NOPE"] = off
    return row


class TestParseFlowRow:
    def test_wide_row_becomes_one_long_row_per_hour(self):
        parsed = parse_flow_row(wide_row(**{"4": (100.0, 20.0), "8": (5000.0, 9000.0)}))
        assert parsed == [
            ("2호선", "강남", "202606", 4, 100.0, 20.0),
            ("2호선", "강남", "202606", 8, 5000.0, 9000.0),
        ]

    def test_hours_are_sorted_numerically_not_lexically(self):
        parsed = parse_flow_row(wide_row(**{"4": (1, 1), "10": (2, 2), "23": (3, 3)}))
        assert [p[3] for p in parsed] == [4, 10, 23]

    def test_line_and_station_are_normalized(self):
        parsed = parse_flow_row(wide_row(line="경부선", station="서울역", **{"8": (1.0, 2.0)}))
        assert parsed[0][0] == "1호선"
        assert parsed[0][1] == "서울"

    def test_missing_and_invalid_values_become_zero(self):
        row = wide_row(**{"8": (None, "")})
        row["HR_9_GET_ON_NOPE"] = "not-a-number"
        row["HR_9_GET_OFF_NOPE"] = 5
        parsed = dict((p[3], (p[4], p[5])) for p in parse_flow_row(row))
        assert parsed[8] == (0.0, 0.0)
        assert parsed[9] == (0.0, 5.0)

    def test_non_hour_columns_are_ignored(self):
        parsed = parse_flow_row(wide_row(**{"8": (1.0, 2.0)}))
        assert len(parsed) == 1

    def test_row_missing_identity_fields_is_dropped(self):
        assert parse_flow_row({"USE_MM": "202606", "HR_8_GET_ON_NOPE": 1}) == []
        assert parse_flow_row(wide_row(station="", **{"8": (1, 1)})) == []
        assert parse_flow_row(wide_row(ym="", **{"8": (1, 1)})) == []

    def test_late_night_hours_after_midnight_are_kept(self):
        # 운행일 기준이라 0~3시가 같은 날짜의 마지막 시간대다.
        parsed = parse_flow_row(wide_row(**{"0": (10, 20), "3": (1, 2)}))
        assert [p[3] for p in parsed] == [0, 3]


class TestAggregateFlowRows:
    def test_same_station_from_different_physical_lines_is_summed(self):
        # 서울역은 1호선(서울교통공사)과 경부선(코레일)에 따로 집계된다.
        rows = [
            ("1호선", "서울", "202606", 8, 100.0, 50.0),
            ("1호선", "서울", "202606", 8, 300.0, 200.0),
        ]
        assert aggregate_flow_rows(rows) == [("1호선", "서울", "202606", 8, 400.0, 250.0)]

    def test_different_hours_are_not_merged(self):
        rows = [
            ("2호선", "강남", "202606", 8, 100.0, 50.0),
            ("2호선", "강남", "202606", 9, 200.0, 60.0),
        ]
        assert len(aggregate_flow_rows(rows)) == 2

    def test_different_lines_are_not_merged(self):
        rows = [
            ("2호선", "강남", "202606", 8, 100.0, 50.0),
            ("신분당선", "강남", "202606", 8, 10.0, 5.0),
        ]
        assert len(aggregate_flow_rows(rows)) == 2

    def test_different_months_are_not_merged(self):
        rows = [
            ("2호선", "강남", "202606", 8, 1.0, 1.0),
            ("2호선", "강남", "202605", 8, 2.0, 2.0),
        ]
        assert len(aggregate_flow_rows(rows)) == 2

    def test_empty_input(self):
        assert aggregate_flow_rows([]) == []

    def test_result_is_primary_key_unique(self):
        rows = [("1호선", "서울", "202606", h, 1.0, 1.0) for h in range(24)] * 3
        aggregated = aggregate_flow_rows(rows)
        keys = [r[:4] for r in aggregated]
        assert len(keys) == len(set(keys)) == 24
        assert all(r[4] == 3.0 for r in aggregated)
