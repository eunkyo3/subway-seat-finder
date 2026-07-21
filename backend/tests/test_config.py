"""설정 로딩 테스트.

설정 주입 경로가 하나(환경변수)라는 것과, 그 경로에서 흔히 깨지는 지점들을 고정한다.
"""

import logging

import pytest

from backend.app import config
from backend.app.config import Settings, best_source, load_settings

ENV_NAMES = (
    "SEOUL_API_KEY",
    "SEOUL_REALTIME_API_KEY",
    "SUBWAY_DB_PATH",
    "SUBWAY_RAW_DIR",
    "SUBWAY_SNAPSHOT_DIR",
    "REALTIME_CACHE_TTL",
    "SIMILAR_THRESHOLD_PCT",
)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """환경변수와 .env 를 모두 걷어낸 상태에서 시작한다."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    # 개발자 로컬의 진짜 .env 가 테스트에 새어 들어오지 않게 없는 경로를 가리킨다.
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "absent.env")
    return tmp_path


def write_env(path, body: str):
    target = path / ".env"
    target.write_text(body, encoding="utf-8")
    return target


class TestEnvFile:
    def test_values_come_from_env_file(self, clean_env, monkeypatch):
        monkeypatch.setattr(config, "ENV_FILE", write_env(clean_env, "SEOUL_API_KEY=FROM-FILE\n"))
        assert load_settings().api_key == "FROM-FILE"

    def test_missing_env_file_is_not_an_error(self, clean_env):
        settings = load_settings()
        assert settings.api_key is None
        assert settings.realtime_enabled is False

    def test_real_environment_wins_over_env_file(self, clean_env, monkeypatch):
        # 배포 환경이 주입한 값이 파일보다 우선해야 한다.
        monkeypatch.setattr(config, "ENV_FILE", write_env(clean_env, "SEOUL_API_KEY=FROM-FILE\n"))
        monkeypatch.setenv("SEOUL_API_KEY", "FROM-SHELL")
        assert load_settings().api_key == "FROM-SHELL"

    def test_comments_and_blank_lines_are_ignored(self, clean_env, monkeypatch):
        body = "# 주석\n\nSEOUL_API_KEY=REAL\n# SEOUL_REALTIME_API_KEY=주석처리됨\n"
        monkeypatch.setattr(config, "ENV_FILE", write_env(clean_env, body))
        settings = load_settings()
        assert settings.api_key == "REAL"
        assert settings.realtime_api_key is None


class TestBlankValues:
    def test_empty_key_counts_as_missing(self, clean_env, monkeypatch):
        # .env.example 을 그대로 복사하면 값이 빈 채로 남는다. 이걸 '설정됨'으로
        # 보면 빈 키로 API 를 때려 엉뚱한 오류를 받는다.
        monkeypatch.setenv("SEOUL_API_KEY", "")
        assert load_settings().api_key is None

    def test_whitespace_only_key_counts_as_missing(self, clean_env, monkeypatch):
        monkeypatch.setenv("SEOUL_REALTIME_API_KEY", "   ")
        settings = load_settings()
        assert settings.realtime_api_key is None
        assert settings.realtime_enabled is False

    def test_surrounding_whitespace_is_trimmed(self, clean_env, monkeypatch):
        # 복붙하면 끝에 공백이나 개행이 붙는다. URL 에 그대로 들어가면 인증이 깨진다.
        monkeypatch.setenv("SEOUL_API_KEY", "  KEY123  ")
        assert load_settings().api_key == "KEY123"

    def test_blank_path_falls_back_to_default(self, clean_env, monkeypatch):
        monkeypatch.setenv("SUBWAY_DB_PATH", "")
        assert load_settings().db_path.name == "subway.duckdb"


class TestNumericSettings:
    def test_defaults(self, clean_env):
        settings = load_settings()
        assert settings.realtime_cache_ttl_sec == 30
        assert settings.similar_threshold_pct == 8.0

    def test_override(self, clean_env, monkeypatch):
        monkeypatch.setenv("REALTIME_CACHE_TTL", "60")
        monkeypatch.setenv("SIMILAR_THRESHOLD_PCT", "12.5")
        settings = load_settings()
        assert settings.realtime_cache_ttl_sec == 60
        assert settings.similar_threshold_pct == 12.5

    def test_garbage_falls_back_to_default_with_warning(self, clean_env, monkeypatch, caplog):
        # 오타 하나로 앱이 아예 안 뜨는 것보다, 기본값으로 뜨고 경고를 남기는 편이 낫다.
        monkeypatch.setenv("REALTIME_CACHE_TTL", "삼십초")
        with caplog.at_level(logging.WARNING):
            settings = load_settings()
        assert settings.realtime_cache_ttl_sec == 30
        assert "REALTIME_CACHE_TTL" in caplog.text


class TestRealtimeEnabled:
    def test_enabled_only_with_a_real_key(self, clean_env, monkeypatch):
        assert load_settings().realtime_enabled is False
        monkeypatch.setenv("SEOUL_REALTIME_API_KEY", "RT-KEY")
        assert load_settings().realtime_enabled is True

    def test_general_key_alone_does_not_enable_realtime(self, clean_env, monkeypatch):
        # 두 키는 호환되지 않는다. 일반 키로 실시간을 부르면 ERROR-338 이다.
        monkeypatch.setenv("SEOUL_API_KEY", "GENERAL")
        assert load_settings().realtime_enabled is False


class TestBestSource:
    def test_official_wins(self):
        assert best_source(["estimated", "official"]) == "official"

    def test_estimated_when_alone(self):
        assert best_source(["estimated"]) == "estimated"

    def test_empty_is_none(self):
        assert best_source([]) == "none"


class TestSettingsShape:
    def test_settings_is_immutable(self):
        settings = Settings(
            api_key="a", realtime_api_key=None, db_path=config.DATA_DIR,
            raw_dir=config.RAW_DIR, snapshot_dir=config.SNAPSHOT_DIR,
            realtime_cache_ttl_sec=30, similar_threshold_pct=8.0,
        )
        with pytest.raises(Exception):
            settings.api_key = "b"  # type: ignore[misc]
