"""capture_snapshots 수집기 테스트.

네트워크와 실시간 클라이언트는 가짜로 바꾸고, 수집기의 책임만 본다:
DB 를 못 잡았을 때의 실패 처리(§A 의 침묵 경로)와 도착정보 수집 범위.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from backend.app.clients.realtime import RealtimeResult
from backend.app.config import Settings
from backend.app.etl import capture_snapshots

NOW = datetime(2026, 7, 22, 8, 0, 0)


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        api_key="general-key",
        realtime_api_key="realtime-key",
        db_path=tmp_path / "subway.duckdb",
        raw_dir=tmp_path / "raw",
        snapshot_dir=tmp_path / "snapshots",
        realtime_cache_ttl_sec=30,
        similar_threshold_pct=8.0,
    )


class FakeClient:
    """호출 기록만 남기는 실시간 클라이언트 대역."""

    last: "FakeClient | None" = None

    def __init__(self, settings: Settings, *, con=None, **_: object) -> None:
        self.settings = settings
        self.con = con
        self.position_lines: list[str] = []
        self.arrival_calls = 0
        FakeClient.last = self

    def fetch_positions(self, line: str) -> RealtimeResult:
        self.position_lines.append(line)
        return RealtimeResult(
            source="live",
            kind="position",
            key=line,
            fetched_at=NOW,
            records=[{"train_no": "2101"}],
            payload={"realtimePositionList": []},
        )

    def fetch_all_arrivals(self) -> RealtimeResult:
        self.arrival_calls += 1
        return RealtimeResult(
            source="live",
            kind="arrival",
            key="ALL",
            fetched_at=NOW,
            records=[{"train_no": "2101"}],
            payload={"realtimeArrivalList": []},
        )

    def save_snapshot(self, result: RealtimeResult) -> Path:
        path = self.settings.snapshot_dir / f"{result.kind}_{result.key}_fake.json"
        path.write_text("{}", encoding="utf-8")
        return path

    def close(self) -> None:
        pass


@pytest.fixture()
def fake_client(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(capture_snapshots, "load_settings", lambda: settings)
    monkeypatch.setattr(capture_snapshots, "RealtimeClient", FakeClient)
    FakeClient.last = None
    return settings


class TestArrivalCollection:
    def test_arrivals_collected_once_per_round(self, fake_client):
        rc = capture_snapshots.main(["--lines", "2호선", "--rounds", "2", "--interval", "0"])

        assert rc == 0
        assert FakeClient.last is not None
        assert FakeClient.last.position_lines == ["2호선", "2호선"]
        assert FakeClient.last.arrival_calls == 2

    def test_no_arrivals_flag_disables_collection(self, fake_client):
        rc = capture_snapshots.main(
            ["--lines", "2호선", "--rounds", "2", "--interval", "0", "--no-arrivals"]
        )

        assert rc == 0
        assert FakeClient.last.arrival_calls == 0


class TestDbUnavailable:
    @pytest.fixture()
    def locked_db(self, fake_client, monkeypatch):
        def raise_locked(path):
            raise duckdb.IOException(f"lock on {path}")

        monkeypatch.setattr(capture_snapshots, "connect", raise_locked)
        return fake_client

    def test_warns_that_origin_correction_stays_disabled(self, locked_db, caplog):
        # 스냅샷 저장은 계속돼야 하지만(발표 안전망), 시발 보정이 죽은 채라는
        # 결과는 반드시 드러나야 한다. 이 침묵이 §A 를 오래 숨겼다.
        with caplog.at_level(logging.WARNING, logger="capture"):
            rc = capture_snapshots.main(["--lines", "2호선", "--rounds", "1"])

        assert rc == 0
        assert "시발" in caplog.text
        assert FakeClient.last.con is None

    def test_require_db_fails_fast(self, locked_db, caplog):
        with caplog.at_level(logging.ERROR, logger="capture"):
            rc = capture_snapshots.main(
                ["--lines", "2호선", "--rounds", "1", "--require-db"]
            )

        assert rc == 1
        assert "--require-db" in caplog.text
        # 실패로 끝났으니 API 호출 자체가 없어야 한다. 한도만 축내면 안 된다.
        assert FakeClient.last is None
