from backend.app.naming import (
    line_from_subway_id,
    normalize_direction,
    normalize_line,
    normalize_station,
    station_key,
    subway_id_from_line,
)


class TestNormalizeLine:
    def test_zero_padded_and_plain_forms_collapse(self):
        assert normalize_line("01호선") == "1호선"
        assert normalize_line("1호선") == "1호선"
        assert normalize_line("  02호선 ") == "2호선"

    def test_branch_and_extension_suffixes_fold_to_main_line(self):
        assert normalize_line("9호선(연장)") == "9호선"
        assert normalize_line("7호선(인천)") == "7호선"
        assert normalize_line("9호선2~3단계") == "9호선"

    def test_physical_lines_map_to_service_lines(self):
        # subwayStationMaster 는 1호선을 경부/경인/경원선으로 쪼개 놓는다.
        assert normalize_line("경부선") == "1호선"
        assert normalize_line("경인선") == "1호선"
        assert normalize_line("경원선") == "1호선"
        assert normalize_line("과천선") == "4호선"
        assert normalize_line("안산선") == "4호선"
        assert normalize_line("일산선") == "3호선"
        assert normalize_line("분당선") == "수인분당선"
        assert normalize_line("수인선") == "수인분당선"
        assert normalize_line("경의선") == "경의중앙선"
        assert normalize_line("중앙선") == "경의중앙선"

    def test_other_operators_are_not_folded_into_seoul_lines(self):
        # 이름에 '1호선'/'2호선'이 들어있지만 서울 호선이 아니다.
        assert normalize_line("인천1호선") == "인천1호선"
        assert normalize_line("인천2호선") == "인천2호선"
        assert normalize_line("인천선") == "인천1호선"
        assert normalize_line("공항철도1호선") == "공항철도"

    def test_empty_input(self):
        assert normalize_line(None) == ""
        assert normalize_line("") == ""
        assert normalize_line("   ") == ""


class TestNormalizeStation:
    def test_strips_parenthetical_annotations(self):
        assert normalize_station("왕십리(성동구청)") == "왕십리"
        assert normalize_station("서울역(1)") == "서울"
        assert normalize_station("이수(총신대입구)") == "이수"

    def test_strips_separators(self):
        assert normalize_station("4·19민주묘지") == "419민주묘지"
        assert normalize_station("동대문역사문화공원") == "동대문역사문화공원"

    def test_trailing_station_suffix_removed(self):
        # 두 소스가 '서울역'/'서울'을 섞어 쓰므로 접미사를 떼어 맞춘다.
        assert normalize_station("서울역") == "서울"
        assert normalize_station("청량리역") == "청량리"

    def test_short_names_keep_suffix(self):
        # 2글자 이하는 '역'을 떼면 이름이 사라지므로 보존한다.
        assert normalize_station("석계") == "석계"
        assert normalize_station("역곡") == "역곡"

    def test_empty_input(self):
        assert normalize_station(None) == ""
        assert normalize_station("") == ""


class TestStationKey:
    def test_key_combines_normalized_line_and_station(self):
        assert station_key("경부선", "서울역") == "1호선|서울"
        assert station_key("02호선", "왕십리(성동구청)") == "2호선|왕십리"

    def test_same_station_from_different_sources_yields_same_key(self):
        assert station_key("01호선", "서울역") == station_key("경부선", "서울")


class TestNormalizeDirection:
    """실시간 API 와 혼잡도 통계가 방향을 다른 말로 부른다. 하나로 모아야 조인이 된다."""

    def test_realtime_codes(self):
        assert normalize_direction("0") == "상선"
        assert normalize_direction("1") == "하선"

    def test_realtime_korean(self):
        assert normalize_direction("상행") == "상선"
        assert normalize_direction("하행") == "하선"

    def test_statistics_vocabulary_passes_through(self):
        assert normalize_direction("상선") == "상선"
        assert normalize_direction("하선") == "하선"

    def test_loop_line_vocabulary(self):
        # 2호선은 상행/하행 대신 내선/외선으로 부른다.
        assert normalize_direction("내선") == "상선"
        assert normalize_direction("외선") == "하선"
        assert normalize_direction("내선순환") == "상선"
        assert normalize_direction("외선순환") == "하선"

    def test_whitespace_is_tolerated(self):
        assert normalize_direction(" 상행 ") == "상선"

    def test_unknown_and_empty(self):
        assert normalize_direction(None) == ""
        assert normalize_direction("") == ""
        assert normalize_direction("알수없음") == ""

    def test_realtime_and_statistics_agree_after_normalizing(self):
        # 이 등식이 깨지면 방향 필터가 조용히 전부 걸러낸다.
        assert normalize_direction("상행") == normalize_direction("상선")
        assert normalize_direction("0") == normalize_direction("내선")


class TestSubwayId:
    def test_round_trip_for_seoul_lines(self):
        for n in range(1, 10):
            line = f"{n}호선"
            sid = subway_id_from_line(line)
            assert sid is not None
            assert line_from_subway_id(sid) == line

    def test_unknown_id_returns_empty(self):
        assert line_from_subway_id("9999") == ""
        assert line_from_subway_id(None) == ""

    def test_accepts_unnormalized_line(self):
        assert subway_id_from_line("02호선") == "1002"
