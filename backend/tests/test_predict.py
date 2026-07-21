"""예측 엔진 테스트 — 기준혼잡도 폴백, 실시간 보정 신호, 착석 타임라인, 추천 판정."""

from datetime import datetime

import duckdb
import pytest

from backend.app.db import init_schema
from backend.app.predict.baseline import (
    day_type_of,
    get_baseline,
    grade_of,
    time_slot_of,
)
from backend.app.predict.engine import (
    VERDICT_SIMILAR,
    VERDICT_TAKE_NEXT,
    VERDICT_TAKE_THIS,
    compare_trains,
    is_predictable,
    predict_train,
)
from backend.app.predict.signals import (
    HEADWAY_FACTOR_RANGE,
    ORIGIN_EMPTY_FACTOR,
    ORIGIN_RECOVERY_STATIONS,
    compute_headway_sec,
    detect_origin,
    headway_factor,
    nominal_headway_sec,
    origin_factor,
)
from backend.app.predict.trajectory import (
    SEAT_AVAILABLE_PCT,
    build_seat_timeline,
)

MORNING = datetime(2026, 7, 21, 8, 15)  # 화요일 08:15 -> 평일 / 08:00 슬롯


@pytest.fixture
def con():
    connection = duckdb.connect(":memory:")
    init_schema(connection)
    yield connection
    connection.close()


def add_station(con, line, name, seq, *, branch_no=0):
    con.execute(
        "INSERT INTO station_master (station_key, name, name_norm, line, seq, branch_no,"
        " lat, lng) VALUES (?,?,?,?,?,?,37.5,127.0)",
        [f"{line}|{name}", name, name, line, seq, branch_no],
    )


def add_congestion(con, line, name, pct, *, slot="08:00", day="평일",
                   direction="상선", source="official"):
    con.execute(
        "INSERT INTO congestion_stat VALUES (?,?,?,?,?,?,?)",
        [line, name, day, direction, slot, pct, source],
    )


class TestGradeAndSlots:
    def test_grade_boundaries(self):
        assert grade_of(0) == "여유"
        assert grade_of(79.9) == "여유"
        assert grade_of(80) == "보통"
        assert grade_of(129.9) == "보통"
        assert grade_of(130) == "혼잡"
        assert grade_of(149.9) == "혼잡"
        assert grade_of(150) == "매우혼잡"

    def test_day_type(self):
        assert day_type_of(datetime(2026, 7, 21)) == "평일"   # 화
        assert day_type_of(datetime(2026, 7, 25)) == "토요일"  # 토
        assert day_type_of(datetime(2026, 7, 26)) == "일요일"  # 일

    def test_time_slot_floors_to_half_hour(self):
        assert time_slot_of(datetime(2026, 7, 21, 8, 0)) == "08:00"
        assert time_slot_of(datetime(2026, 7, 21, 8, 29)) == "08:00"
        assert time_slot_of(datetime(2026, 7, 21, 8, 30)) == "08:30"
        assert time_slot_of(datetime(2026, 7, 21, 8, 59)) == "08:30"
        assert time_slot_of(datetime(2026, 7, 21, 0, 5)) == "00:00"


class TestBaselineFallback:
    def test_exact_match(self, con):
        add_congestion(con, "2호선", "강남", 150.0)
        result = get_baseline(con, "2호선", "강남", "평일", "08:00", "상선")
        assert (result.congestion_pct, result.resolution) == (150.0, "exact")

    def test_official_beats_estimated(self, con):
        add_congestion(con, "2호선", "강남", 150.0, source="official")
        add_congestion(con, "2호선", "강남", 90.0, source="estimated")
        result = get_baseline(con, "2호선", "강남", "평일", "08:00", "상선")
        assert result.congestion_pct == 150.0
        assert result.source == "official"
        assert result.is_estimated is False

    def test_estimated_used_when_no_official(self, con):
        add_congestion(con, "2호선", "강남", 90.0, source="estimated")
        result = get_baseline(con, "2호선", "강남", "평일", "08:00", "상선")
        assert (result.congestion_pct, result.source) == (90.0, "estimated")
        assert result.is_estimated is True

    def test_unknown_direction_averages_over_directions(self, con):
        add_congestion(con, "2호선", "강남", 100.0, direction="상선")
        add_congestion(con, "2호선", "강남", 200.0, direction="하선")
        result = get_baseline(con, "2호선", "강남", "평일", "08:00", "서쪽")
        assert result.congestion_pct == 150.0
        assert result.resolution == "direction"

    def test_missing_station_falls_back_to_line_slot_average(self, con):
        add_congestion(con, "2호선", "강남", 100.0)
        add_congestion(con, "2호선", "역삼", 200.0)
        result = get_baseline(con, "2호선", "없는역", "평일", "08:00", "상선")
        assert result.congestion_pct == 150.0
        assert result.resolution == "line_slot"

    def test_missing_slot_falls_back_to_line_average(self, con):
        add_congestion(con, "2호선", "강남", 100.0, slot="08:00")
        add_congestion(con, "2호선", "강남", 50.0, slot="14:00")
        result = get_baseline(con, "2호선", "없는역", "평일", "22:00", "상선")
        assert result.congestion_pct == 75.0
        assert result.resolution == "line"

    def test_line_without_statistics_returns_none_resolution(self, con):
        result = get_baseline(con, "9호선", "노량진", "평일", "08:00", "상선")
        assert (result.congestion_pct, result.source, result.resolution) == (0.0, "none", "none")

    def test_line_name_is_normalized_before_lookup(self, con):
        add_congestion(con, "1호선", "서울", 120.0)
        # 실시간 API 는 '01호선', 좌표 소스는 '경부선' 으로 부른다.
        assert get_baseline(con, "경부선", "서울역", "평일", "08:00", "상선").congestion_pct == 120.0


class TestHeadwaySignal:
    def test_factor_is_monotonically_increasing_in_headway(self):
        factors = [headway_factor(gap, 8).factor for gap in range(30, 900, 30)]
        assert factors == sorted(factors)
        assert factors[0] < factors[-1]

    def test_nominal_headway_gives_neutral_factor(self):
        nominal = nominal_headway_sec(8)
        assert headway_factor(nominal, 8).factor == pytest.approx(1.0)

    def test_wider_gap_increases_congestion(self):
        nominal = nominal_headway_sec(8)
        assert headway_factor(nominal * 2, 8).factor > 1.0

    def test_tighter_gap_decreases_congestion(self):
        nominal = nominal_headway_sec(8)
        assert headway_factor(nominal * 0.5, 8).factor < 1.0

    def test_factor_is_clamped_to_range(self):
        assert headway_factor(10_000, 8).factor == HEADWAY_FACTOR_RANGE[1]
        assert headway_factor(1, 8).factor == HEADWAY_FACTOR_RANGE[0]

    def test_unknown_headway_is_neutral_and_flagged_unavailable(self):
        for value in (None, 0, -5):
            signal = headway_factor(value, 8)
            assert signal.factor == 1.0
            assert signal.available is False

    def test_peak_hour_has_tighter_nominal_headway_than_midday(self):
        assert nominal_headway_sec(8) < nominal_headway_sec(14)


class TestComputeHeadway:
    def test_gap_to_preceding_train(self):
        arrivals = [("A", 120), ("B", 400), ("C", 700)]
        assert compute_headway_sec(arrivals, "B") == 280

    def test_leading_train_has_no_preceding_gap(self):
        assert compute_headway_sec([("A", 120), ("B", 400)], "A") is None

    def test_unsorted_input_is_ordered_first(self):
        arrivals = [("C", 700), ("A", 120), ("B", 400)]
        assert compute_headway_sec(arrivals, "B") == 280

    def test_unknown_train_returns_none(self):
        assert compute_headway_sec([("A", 120)], "Z") is None

    def test_empty_input(self):
        assert compute_headway_sec([], "A") is None


class TestOriginSignal:
    def test_mid_line_origin_starts_nearly_empty(self):
        assert origin_factor(0, is_mid_line_origin=True).factor == ORIGIN_EMPTY_FACTOR

    def test_factor_recovers_toward_one_with_distance(self):
        factors = [
            origin_factor(n, is_mid_line_origin=True).factor
            for n in range(0, ORIGIN_RECOVERY_STATIONS + 1)
        ]
        assert factors == sorted(factors)
        assert factors[-1] == pytest.approx(1.0)

    def test_beyond_recovery_span_is_neutral(self):
        assert origin_factor(50, is_mid_line_origin=True).factor == pytest.approx(1.0)

    def test_terminal_origin_is_not_corrected(self):
        # 종점 시발은 이미 통계에 반영돼 있다.
        signal = origin_factor(0, is_mid_line_origin=False)
        assert signal.factor == 1.0
        assert signal.is_mid_line_origin is False

    def test_unknown_distance_is_neutral(self):
        assert origin_factor(None, is_mid_line_origin=True).factor == 1.0


class TestDetectOrigin:
    def test_first_seen_at_mid_line_station_is_mid_line_origin(self):
        history = [
            ("군자", datetime(2026, 7, 21, 8, 0)),
            ("장한평", datetime(2026, 7, 21, 8, 3)),
        ]
        is_mid, first = detect_origin(history, {"성수", "신설동"})
        assert (is_mid, first) == (True, "군자")

    def test_first_seen_at_terminal_is_not_mid_line_origin(self):
        history = [
            ("성수", datetime(2026, 7, 21, 8, 0)),
            ("건대입구", datetime(2026, 7, 21, 8, 3)),
        ]
        assert detect_origin(history, {"성수", "신설동"})[0] is False

    def test_out_of_order_history_uses_earliest_timestamp(self):
        history = [
            ("장한평", datetime(2026, 7, 21, 8, 3)),
            ("군자", datetime(2026, 7, 21, 8, 0)),
        ]
        assert detect_origin(history, set())[1] == "군자"

    def test_empty_history(self):
        assert detect_origin([], {"성수"}) == (False, None)


class TestIsPredictable:
    def test_seoul_metro_lines_are_predictable(self):
        for n in range(1, 9):
            assert is_predictable(f"{n}호선") is True

    def test_other_lines_are_not(self):
        for line in ("9호선", "경의중앙선", "신분당선", "공항철도", "인천1호선"):
            assert is_predictable(line) is False

    def test_line_name_is_normalized(self):
        assert is_predictable("02호선") is True
        assert is_predictable("경부선") is True


class TestPredictTrain:
    def test_signals_multiply_onto_the_baseline(self, con):
        add_congestion(con, "2호선", "강남", 100.0)
        nominal = nominal_headway_sec(8)
        prediction = predict_train(
            con, line="2호선", station="강남", when=MORNING, direction="상선",
            headway_sec=nominal * 2, stations_since_origin=0, is_mid_line_origin=True,
        )
        expected = 100.0 * headway_factor(nominal * 2, 8).factor * ORIGIN_EMPTY_FACTOR
        assert prediction.expected_pct == pytest.approx(round(expected, 1))

    def test_no_signals_means_expected_equals_baseline(self, con):
        add_congestion(con, "2호선", "강남", 137.0)
        prediction = predict_train(
            con, line="2호선", station="강남", when=MORNING, direction="상선"
        )
        assert prediction.expected_pct == 137.0
        assert prediction.baseline_pct == 137.0
        assert prediction.load_factor == pytest.approx(1.0)

    def test_grade_reflects_expected_not_baseline(self, con):
        add_congestion(con, "2호선", "강남", 160.0)
        prediction = predict_train(
            con, line="2호선", station="강남", when=MORNING, direction="상선",
            stations_since_origin=0, is_mid_line_origin=True,
        )
        assert prediction.baseline_pct == 160.0
        assert prediction.expected_pct == 40.0
        assert prediction.grade == "여유"

    def test_reasons_explain_the_corrections(self, con):
        add_congestion(con, "2호선", "강남", 100.0)
        prediction = predict_train(
            con, line="2호선", station="강남", when=MORNING, direction="상선",
            headway_sec=nominal_headway_sec(8) * 2,
            stations_since_origin=1, is_mid_line_origin=True,
        )
        joined = " ".join(prediction.reasons)
        assert "간격" in joined
        assert "시발" in joined

    def test_estimated_source_is_disclosed(self, con):
        add_congestion(con, "2호선", "강남", 100.0, source="estimated")
        prediction = predict_train(
            con, line="2호선", station="강남", when=MORNING, direction="상선"
        )
        assert any("추정치" in r for r in prediction.reasons)

    def test_fallback_resolution_is_disclosed(self, con):
        add_congestion(con, "2호선", "역삼", 100.0)
        prediction = predict_train(
            con, line="2호선", station="강남", when=MORNING, direction="상선"
        )
        assert prediction.baseline_resolution != "exact"
        assert any("통계가 없어" in r for r in prediction.reasons)

    def test_load_factor_carries_the_correction(self, con):
        add_congestion(con, "2호선", "강남", 100.0)
        prediction = predict_train(
            con, line="2호선", station="강남", when=MORNING, direction="상선",
            stations_since_origin=0, is_mid_line_origin=True,
        )
        assert prediction.load_factor == pytest.approx(ORIGIN_EMPTY_FACTOR, rel=1e-3)


class TestCompareTrains:
    def _prediction(self, con, pct, eta=None):
        add_congestion(con, "2호선", f"S{pct}", pct)
        return predict_train(
            con, line="2호선", station=f"S{pct}", when=MORNING,
            direction="상선", eta_sec=eta,
        )

    def test_recommends_next_when_meaningfully_emptier(self, con):
        this_train = self._prediction(con, 160.0, eta=60)
        next_train = self._prediction(con, 100.0, eta=420)
        result = compare_trains(this_train, next_train, similar_threshold_pct=8)
        assert result.verdict == VERDICT_TAKE_NEXT
        assert result.difference_pct == 60.0
        assert "6분" in result.message

    def test_recommends_this_when_next_is_worse(self, con):
        this_train = self._prediction(con, 100.0)
        next_train = self._prediction(con, 160.0)
        result = compare_trains(this_train, next_train, similar_threshold_pct=8)
        assert result.verdict == VERDICT_TAKE_THIS

    def test_similar_when_difference_below_threshold(self, con):
        this_train = self._prediction(con, 120.0)
        next_train = self._prediction(con, 125.0)
        result = compare_trains(this_train, next_train, similar_threshold_pct=8)
        assert result.verdict == VERDICT_SIMILAR
        assert "비슷" in result.message

    def test_difference_exactly_at_threshold_triggers_a_recommendation(self, con):
        # 임계값 '미만'일 때만 비슷함이므로, 정확히 임계값이면 추천이 나온다.
        this_train = self._prediction(con, 128.0)
        next_train = self._prediction(con, 120.0)
        assert compare_trains(
            this_train, next_train, similar_threshold_pct=8
        ).verdict == VERDICT_TAKE_NEXT

    def test_difference_just_below_threshold_stays_similar(self, con):
        this_train = self._prediction(con, 127.9)
        next_train = self._prediction(con, 120.0)
        assert compare_trains(
            this_train, next_train, similar_threshold_pct=8
        ).verdict == VERDICT_SIMILAR

    def test_missing_next_train_defaults_to_this(self, con):
        this_train = self._prediction(con, 120.0)
        result = compare_trains(this_train, None, similar_threshold_pct=8)
        assert result.verdict == VERDICT_TAKE_THIS
        assert result.next_train is None


class TestSeatTimeline:
    """선형 노선 기준. 순환선 경로 선택은 TestLoopLinePath 에서 따로 본다."""

    def _line(self, con, congestions):
        for seq, (name, pct) in enumerate(congestions, start=1):
            add_station(con, "5호선", name, seq)
            for slot in ("08:00", "08:30", "09:00"):
                add_congestion(con, "5호선", name, pct, slot=slot)

    def test_timeline_covers_origin_to_destination_inclusive(self, con):
        self._line(con, [("A", 150), ("B", 120), ("C", 90), ("D", 30)])
        timeline = build_seat_timeline(
            con, "5호선", "A", "D", departure=MORNING, direction="상선"
        )
        assert [s.name for s in timeline.stops] == ["A", "B", "C", "D"]

    def test_reverse_direction_walks_backwards(self, con):
        self._line(con, [("A", 150), ("B", 120), ("C", 90), ("D", 30)])
        timeline = build_seat_timeline(
            con, "5호선", "D", "A", departure=MORNING, direction="하선"
        )
        assert [s.name for s in timeline.stops] == ["D", "C", "B", "A"]

    def test_seat_index_is_first_stop_below_threshold(self, con):
        self._line(con, [("A", 150), ("B", 120), ("C", 30), ("D", 20)])
        timeline = build_seat_timeline(
            con, "5호선", "A", "D", departure=MORNING, direction="상선"
        )
        assert timeline.seat_from is not None
        assert timeline.seat_from.name == "C"
        assert timeline.seat_from.congestion_pct < SEAT_AVAILABLE_PCT

    def test_no_seat_when_crowded_the_whole_way(self, con):
        self._line(con, [("A", 150), ("B", 150), ("C", 150)])
        timeline = build_seat_timeline(
            con, "5호선", "A", "C", departure=MORNING, direction="상선"
        )
        assert timeline.seat_from_index is None

    def test_origin_is_never_the_seat_stop(self, con):
        # 방금 탄 역이 한산해도 "여기서부터 앉으세요"는 무의미하다.
        self._line(con, [("A", 10), ("B", 10)])
        timeline = build_seat_timeline(
            con, "5호선", "A", "B", departure=MORNING, direction="상선"
        )
        assert timeline.seat_from_index == 1

    def test_minutes_accumulate_along_the_path(self, con):
        self._line(con, [("A", 100), ("B", 100), ("C", 100)])
        timeline = build_seat_timeline(
            con, "5호선", "A", "C", departure=MORNING, direction="상선"
        )
        assert [s.minutes_from_now for s in timeline.stops] == [0, 2, 4]

    def test_time_slot_advances_for_long_trips(self, con):
        # 20정거장이면 40분이 걸려 다음 통계 슬롯으로 넘어가야 한다.
        self._line(con, [(f"S{i}", 100) for i in range(20)])
        timeline = build_seat_timeline(
            con, "5호선", "S0", "S19", departure=MORNING, direction="상선"
        )
        assert timeline.stops[0].time_slot == "08:00"
        assert timeline.stops[-1].time_slot == "08:30"

    def test_load_factor_scales_the_whole_trip(self, con):
        self._line(con, [("A", 100), ("B", 100)])
        halved = build_seat_timeline(
            con, "5호선", "A", "B", departure=MORNING, direction="상선", load_factor=0.5
        )
        assert [s.congestion_pct for s in halved.stops] == [50.0, 50.0]

    def test_branch_stations_are_excluded_from_the_path(self, con):
        self._line(con, [("A", 100), ("B", 100), ("C", 100)])
        add_station(con, "5호선", "Z", 2, branch_no=1)
        timeline = build_seat_timeline(
            con, "5호선", "A", "C", departure=MORNING, direction="상선"
        )
        assert "Z" not in [s.name for s in timeline.stops]

    def test_unknown_station_yields_empty_timeline(self, con):
        self._line(con, [("A", 100), ("B", 100)])
        assert build_seat_timeline(
            con, "5호선", "A", "없는역", departure=MORNING
        ).stops == []

    def test_same_origin_and_destination_yields_empty_timeline(self, con):
        self._line(con, [("A", 100), ("B", 100)])
        assert build_seat_timeline(con, "5호선", "A", "A", departure=MORNING).stops == []


class TestLoopLinePath:
    """2호선은 순환선이라 양방향으로 갈 수 있다. 짧은 쪽을 골라야 한다."""

    def _loop(self, con, count=10):
        for seq in range(1, count + 1):
            add_station(con, "2호선", f"S{seq}", seq)
            add_congestion(con, "2호선", f"S{seq}", 100.0)

    def test_wrapping_backwards_is_preferred_when_shorter(self, con):
        self._loop(con)
        # S1 -> S10 은 앞으로 9정거장, 뒤로 감으면 1정거장이다.
        timeline = build_seat_timeline(
            con, "2호선", "S1", "S10", departure=MORNING, direction="상선"
        )
        assert [s.name for s in timeline.stops] == ["S1", "S10"]

    def test_forward_is_used_when_shorter(self, con):
        self._loop(con)
        timeline = build_seat_timeline(
            con, "2호선", "S1", "S3", departure=MORNING, direction="상선"
        )
        assert [s.name for s in timeline.stops] == ["S1", "S2", "S3"]

    def test_wrapping_forwards_past_the_end(self, con):
        self._loop(con)
        # S9 -> S2 는 앞으로 감으면 S10 을 거쳐 3정거장, 뒤로는 7정거장이다.
        timeline = build_seat_timeline(
            con, "2호선", "S9", "S2", departure=MORNING, direction="상선"
        )
        assert [s.name for s in timeline.stops] == ["S9", "S10", "S1", "S2"]

    def test_half_way_around_is_deterministic(self, con):
        self._loop(con, count=10)
        # 정확히 반대편이면 어느 쪽이든 같은 길이다. 앞쪽을 택해 결과가 흔들리지 않게 한다.
        timeline = build_seat_timeline(
            con, "2호선", "S1", "S6", departure=MORNING, direction="상선"
        )
        assert [s.name for s in timeline.stops] == ["S1", "S2", "S3", "S4", "S5", "S6"]

    def test_linear_line_never_wraps(self, con):
        for seq in range(1, 11):
            add_station(con, "5호선", f"L{seq}", seq)
            add_congestion(con, "5호선", f"L{seq}", 100.0)
        # 5호선은 순환선이 아니므로 L1 -> L10 은 반드시 9정거장을 모두 지난다.
        timeline = build_seat_timeline(
            con, "5호선", "L1", "L10", departure=MORNING, direction="상선"
        )
        assert len(timeline.stops) == 10
