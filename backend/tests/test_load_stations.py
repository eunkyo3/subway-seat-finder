"""역 마스터 병합 로직 테스트.

실제 API 응답에서 확인된 함정들을 회귀 테스트로 고정한다.
- 좌표 소스는 노선을 물리 선로(경부선/경인선/경원선)로 표기해 서비스 노선과 어긋난다
- 경원선은 한 선로가 두 서비스 노선(1호선/경의중앙선)에 걸쳐 있다
- 같은 이름의 진짜 다른 역이 존재한다 (5호선 양평 vs 경의중앙선 양평, 0.6도 거리)
"""

import duckdb

from backend.app.db import bulk_insert, init_schema
from backend.app.etl.load_stations import (
    SAME_STATION_DEGREES,
    build_coord_index,
    build_station_rows,
    lookup_coord,
)


# build_station_rows 가 돌려주는 튜플의 컬럼 위치. 스키마가 늘면 여기만 고친다.
SEQ = 7
BRANCH_NO = 8
TRANSFER_YN = 11

STATION_COLUMNS = [
    "station_key", "station_id", "name", "name_norm", "name_eng", "line",
    "subway_id", "seq", "branch_no", "lat", "lng", "transfer_yn",
]


def master(name, route, lat, lng):
    return {"BLDN_NM": name, "ROUTE": route, "LAT": str(lat), "LOT": str(lng)}


def stn(name, line, fr_code, cd=None, eng=None):
    return {
        "STATION_NM": name,
        "LINE_NUM": line,
        "FR_CODE": fr_code,
        "STATION_CD": cd,
        "STATION_NM_ENG": eng,
    }


class TestBuildCoordIndex:
    def test_precise_index_keyed_by_service_line(self):
        precise, _ = build_coord_index([master("시청", "02호선", 37.5636, 126.9754)])
        assert precise[("2호선", "시청")] == (37.5636, 126.9754)

    def test_physical_line_is_mapped_to_service_line(self):
        # 좌표 소스의 '경부선'은 서비스 노선 1호선이다.
        precise, _ = build_coord_index([master("노량진", "경부선", 37.514, 126.942)])
        assert ("1호선", "노량진") in precise

    def test_nearby_duplicates_collapse_into_fallback(self):
        # 환승역은 출입구별로 좌표가 조금씩 다르지만 같은 역이다.
        rows = [
            master("신촌", "02호선", 37.555131, 126.936926),
            master("신촌", "경의선", 37.559733, 126.942597),
        ]
        _, fallback = build_coord_index(rows)
        assert "신촌" in fallback

    def test_far_apart_duplicates_are_excluded_from_fallback(self):
        # 5호선 양평과 경의중앙선 양평은 이름만 같은 완전히 다른 역이다.
        rows = [
            master("양평", "05호선", 37.525569, 126.886129),
            master("양평", "중앙선", 37.492773, 127.491837),
        ]
        _, fallback = build_coord_index(rows)
        assert "양평" not in fallback

    def test_threshold_boundary(self):
        base = 37.5
        rows = [
            master("가", "02호선", base, 127.0),
            master("가", "05호선", base + SAME_STATION_DEGREES / 2, 127.0),
        ]
        _, fallback = build_coord_index(rows)
        assert "가" in fallback

        rows = [
            master("나", "02호선", base, 127.0),
            master("나", "05호선", base + SAME_STATION_DEGREES * 2, 127.0),
        ]
        _, fallback = build_coord_index(rows)
        assert "나" not in fallback

    def test_rows_without_coordinates_are_ignored(self):
        precise, fallback = build_coord_index(
            [{"BLDN_NM": "빈역", "ROUTE": "02호선", "LAT": "", "LOT": ""}]
        )
        assert precise == {}
        assert fallback == {}

    def test_parenthetical_alias_is_indexed(self):
        # '왕십리(성동구청)' 은 '왕십리' 로도 '성동구청' 으로도 찾을 수 있어야 한다.
        precise, _ = build_coord_index([master("왕십리(성동구청)", "02호선", 37.5612, 127.0369)])
        assert ("2호선", "왕십리") in precise
        assert ("2호선", "성동구청") in precise


class TestLookupCoord:
    def test_precise_match_wins_over_fallback(self):
        index = build_coord_index(
            [
                master("양평", "05호선", 37.525569, 126.886129),
                master("양평", "중앙선", 37.492773, 127.491837),
            ]
        )
        assert lookup_coord(index, "5호선", "양평") == (37.525569, 126.886129)
        assert lookup_coord(index, "경의중앙선", "양평") == (37.492773, 127.491837)

    def test_fallback_absorbs_physical_line_misfiling(self):
        # 좌표 소스는 서빙고를 경원선(->1호선)으로 두지만 실제로는 경의중앙선 역이다.
        index = build_coord_index([master("서빙고", "경원선", 37.5196, 126.9955)])
        assert lookup_coord(index, "경의중앙선", "서빙고") == (37.5196, 126.9955)

    def test_alias_resolves_differing_primary_names(self):
        # 척추는 '자양', 좌표 소스는 '뚝섬유원지' 를 대표명으로 쓴다.
        index = build_coord_index([master("뚝섬유원지", "07호선", 37.5315, 127.0669)])
        assert lookup_coord(index, "7호선", "자양") is not None

    def test_unknown_station_returns_none(self):
        index = build_coord_index([master("시청", "02호선", 37.5636, 126.9754)])
        assert lookup_coord(index, "2호선", "없는역") is None

    def test_ambiguous_name_without_line_match_returns_none(self):
        index = build_coord_index(
            [
                master("양평", "05호선", 37.525569, 126.886129),
                master("양평", "중앙선", 37.492773, 127.491837),
            ]
        )
        # 어느 쪽 좌표인지 정할 수 없으면 틀린 좌표를 붙이느니 비운다.
        assert lookup_coord(index, "9호선", "양평") is None


class TestBuildStationRows:
    def test_seq_follows_fr_code_not_input_order(self):
        # 입력 순서를 뒤섞어도 FR_CODE 순서(=지리적 순서)대로 seq 가 매겨져야 한다.
        rows = [
            stn("서울역", "01호선", "133"),
            stn("소요산", "01호선", "100"),
            stn("청량리", "01호선", "124"),
        ]
        index = build_coord_index(
            [
                master("서울역", "경부선", 37.5562, 126.9721),
                master("소요산", "경원선", 37.9481, 127.0611),
                master("청량리", "경원선", 37.5800, 127.0483),
            ]
        )
        built = build_station_rows(rows, index)
        assert [(r[7], r[2]) for r in built] == [(1, "소요산"), (2, "청량리"), (3, "서울역")]

    def test_branch_suffix_orders_after_its_main_station(self):
        rows = [
            stn("성수", "02호선", "211"),
            stn("신답", "02호선", "211-2"),
            stn("용답", "02호선", "211-1"),
            stn("건대입구", "02호선", "212"),
        ]
        index = build_coord_index(
            [
                master(n, "02호선", 37.5 + i / 100, 127.0)
                for i, n in enumerate(["성수", "신답", "용답", "건대입구"])
            ]
        )
        built = build_station_rows(rows, index)
        assert [r[2] for r in built] == ["성수", "용답", "신답", "건대입구"]

    def test_transfer_flag_set_when_name_spans_multiple_lines(self):
        rows = [stn("강남", "02호선", "222"), stn("강남", "신분당선", "D07")]
        index = build_coord_index(
            [master("강남", "02호선", 37.4979, 127.0276), master("강남", "신분당선", 37.4979, 127.0276)]
        )
        built = build_station_rows(rows, index)
        assert all(r[TRANSFER_YN] is True for r in built)

    def test_single_line_station_is_not_a_transfer(self):
        rows = [stn("한양대", "02호선", "209")]
        index = build_coord_index([master("한양대", "02호선", 37.5555, 127.0435)])
        assert build_station_rows(rows, index)[0][TRANSFER_YN] is False

    def test_branch_number_identifies_the_branch_not_the_position(self):
        # 한 지선의 역들은 같은 branch_no 로 묶여야 지선 단위 계산이 가능하다.
        # FR_CODE 뒤 숫자(1,2,3)는 지선 '안에서의 순번'이라 식별자가 될 수 없다.
        rows = [
            stn("성수", "02호선", "211"),
            stn("용답", "02호선", "211-1"),
            stn("신답", "02호선", "211-2"),
            stn("신설동", "02호선", "211-4"),
            stn("도림천", "02호선", "234-1"),
            stn("까치산", "02호선", "234-4"),
        ]
        index = build_coord_index(
            [master(n, "02호선", 37.5 + i / 100, 127.0) for i, n in enumerate(
                ["성수", "용답", "신답", "신설동", "도림천", "까치산"]
            )]
        )
        built = {r[2]: r[BRANCH_NO] for r in build_station_rows(rows, index)}
        assert built["성수"] == 0                      # 본선
        assert built["용답"] == built["신답"] == built["신설동"] == 211   # 성수지선
        assert built["도림천"] == built["까치산"] == 234                 # 신정지선
        assert built["용답"] != built["도림천"]         # 서로 다른 지선

    def test_station_without_coordinates_is_dropped(self):
        rows = [stn("있는역", "02호선", "201"), stn("없는역", "02호선", "202")]
        index = build_coord_index([master("있는역", "02호선", 37.5, 127.0)])
        built = build_station_rows(rows, index)
        assert [r[2] for r in built] == ["있는역"]

    def test_unparseable_fr_code_is_dropped(self):
        rows = [stn("정상", "02호선", "201"), stn("깨짐", "02호선", "")]
        index = build_coord_index(
            [master("정상", "02호선", 37.5, 127.0), master("깨짐", "02호선", 37.6, 127.1)]
        )
        assert [r[2] for r in build_station_rows(rows, index)] == ["정상"]

    def test_seq_restarts_per_line(self):
        rows = [stn("시청", "02호선", "201"), stn("서울역", "01호선", "133")]
        index = build_coord_index(
            [master("시청", "02호선", 37.5636, 126.9754), master("서울역", "경부선", 37.5562, 126.9721)]
        )
        built = build_station_rows(rows, index)
        assert {r[5]: r[7] for r in built} == {"1호선": 1, "2호선": 1}

    def test_rows_are_insertable_into_schema(self):
        rows = [stn("시청", "02호선", "201", cd="0201", eng="City Hall")]
        index = build_coord_index([master("시청", "02호선", 37.5636, 126.9754)])
        built = build_station_rows(rows, index)

        con = duckdb.connect(":memory:")
        init_schema(con)
        bulk_insert(con, "station_master", STATION_COLUMNS, built)
        stored = con.execute(
            "SELECT station_key, line, seq, branch_no, name_eng, lat FROM station_master"
        ).fetchone()
        assert stored == ("2호선|시청", "2호선", 1, 0, "City Hall", 37.5636)

    def test_duplicate_station_in_same_line_keeps_first(self):
        rows = [stn("시청", "02호선", "201"), stn("시청", "02호선", "250")]
        index = build_coord_index([master("시청", "02호선", 37.5636, 126.9754)])
        assert len(build_station_rows(rows, index)) == 1
