"""역명 후보 생성과 FR_CODE 순서 파싱 테스트."""

from backend.app.naming import parse_station_order, station_candidates


class TestStationCandidates:
    def test_plain_name(self):
        assert station_candidates("강남") == ["강남"]

    def test_parenthetical_yields_both_sides(self):
        # 소스마다 괄호 바깥/안쪽 중 어느 쪽이든 대표명으로 쓸 수 있다.
        assert station_candidates("왕십리(성동구청)") == ["왕십리", "성동구청"]
        assert station_candidates("이수(총신대입구)") == ["이수", "총신대입구"]

    def test_alias_is_appended(self):
        assert "뚝섬유원지" in station_candidates("자양")
        assert "지제" in station_candidates("평택지제")

    def test_candidates_are_deduplicated_and_ordered(self):
        result = station_candidates("서울역(1)")
        assert result[0] == "서울"
        assert len(result) == len(set(result))

    def test_empty_input(self):
        assert station_candidates(None) == []
        assert station_candidates("") == []
        assert station_candidates("   ") == []


class TestParseStationOrder:
    def test_plain_number(self):
        assert parse_station_order("133") == ("", 133, 0)

    def test_branch_suffix(self):
        assert parse_station_order("211-3") == ("", 211, 3)

    def test_lettered_code(self):
        assert parse_station_order("A04") == ("A", 4, 0)
        assert parse_station_order("d11") == ("D", 11, 0)

    def test_sorting_places_branch_after_main(self):
        codes = ["212", "211-2", "211", "211-1"]
        assert sorted(codes, key=parse_station_order) == ["211", "211-1", "211-2", "212"]

    def test_lettered_and_numeric_sort_into_separate_groups(self):
        codes = ["A02", "133", "A01", "100"]
        ordered = sorted(codes, key=parse_station_order)
        assert ordered == ["100", "133", "A01", "A02"]

    def test_invalid_returns_none(self):
        assert parse_station_order("") is None
        assert parse_station_order(None) is None
        assert parse_station_order("---") is None
