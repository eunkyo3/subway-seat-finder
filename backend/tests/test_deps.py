"""앱 기동 상태 조립 테스트.

앱은 DB 를 읽기 전용으로만 여는데, 그 대가로 위치 로그를 스스로 쌓지 못한다.
로그가 빈 채 뜨면 시발 보정이 통째로 죽는다는 사실(§A)이 기동 로그에
드러나는지를 본다.
"""

from __future__ import annotations

import logging
from pathlib import Path

from backend.app.config import Settings
from backend.app.db import session
from backend.app.deps import build_state

NOW_ROW = (
    "1002", "2101", "1002000230", "강남", "상선",
    False, "성수", "도착", "2026-07-22 08:00:00", "2026-07-22 08:00:05",
)


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        api_key="general-key",
        realtime_api_key=None,
        db_path=tmp_path / "subway.duckdb",
        raw_dir=tmp_path / "raw",
        snapshot_dir=tmp_path / "snapshots",
        realtime_cache_ttl_sec=30,
        similar_threshold_pct=8.0,
    )


def create_db(settings: Settings, *, with_position_log: bool) -> None:
    with session(settings.db_path) as con:
        if with_position_log:
            con.execute(
                "INSERT INTO train_position_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                list(NOW_ROW),
            )


class TestOriginLogStartupWarning:
    def test_empty_position_log_warns_origin_disabled(self, tmp_path, caplog):
        settings = make_settings(tmp_path)
        create_db(settings, with_position_log=False)

        with caplog.at_level(logging.WARNING, logger="backend.app.deps"):
            state = build_state(settings)
        state.close()

        assert "시발" in caplog.text

    def test_populated_position_log_does_not_warn(self, tmp_path, caplog):
        settings = make_settings(tmp_path)
        create_db(settings, with_position_log=True)

        with caplog.at_level(logging.WARNING, logger="backend.app.deps"):
            state = build_state(settings)
        state.close()

        assert "시발" not in caplog.text
