"""Configuration parsing and pagination-settings wiring.

Guards two footguns found in the pre-submission audit: pydantic-settings JSON-decoding of
list-typed env vars (which crashed startup on the documented comma-separated CORS syntax and
even on a bare empty value), and page-size settings that a hardcoded pagination dependency
used to ignore silently.
"""

from __future__ import annotations

import pytest

from app.api.deps import pagination
from app.config import Settings
from app.errors import ValidationError


def _settings(**overrides) -> Settings:
    # _env_file=None keeps these hermetic regardless of a local .env.
    return Settings(_env_file=None, portal_username="x", portal_password="x", **overrides)


class TestCorsOriginsParsing:
    """CORS_ALLOW_ORIGINS must accept the forms the docs promise, not crash on them."""

    def test_empty_string_is_no_origins(self, monkeypatch):
        monkeypatch.setenv("CORS_ALLOW_ORIGINS", "")
        assert _settings().cors_allow_origins == []

    def test_comma_separated_is_split_and_trimmed(self, monkeypatch):
        monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://a.com, https://b.com")
        assert _settings().cors_allow_origins == ["https://a.com", "https://b.com"]

    def test_json_array_is_accepted(self, monkeypatch):
        monkeypatch.setenv("CORS_ALLOW_ORIGINS", '["https://c.com"]')
        assert _settings().cors_allow_origins == ["https://c.com"]

    def test_absent_defaults_to_empty(self, monkeypatch):
        monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
        assert _settings().cors_allow_origins == []

    def test_programmatic_list_passes_through(self):
        assert _settings(cors_allow_origins=["https://d.com"]).cors_allow_origins == [
            "https://d.com"
        ]


class TestBlankEnvValuesAreIgnored:
    """A .env imported into a host creates present-but-empty vars; those must not crash
    startup by failing to coerce '' into a typed field - the default should apply instead."""

    def test_blank_int_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("SNAPSHOT_TTL_SECONDS", "")
        assert _settings().snapshot_ttl_seconds == 300

    def test_blank_bool_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("SNAPSHOT_REFRESH_ON_START", "")
        assert _settings().snapshot_refresh_on_start is True

    def test_blank_validated_str_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "")
        assert _settings().log_format == "json"

    def test_a_real_value_is_still_honored(self, monkeypatch):
        monkeypatch.setenv("SNAPSHOT_TTL_SECONDS", "999")
        assert _settings().snapshot_ttl_seconds == 999


class TestPaginationHonoursSettings:
    """DEFAULT_PAGE_SIZE / MAX_PAGE_SIZE are real knobs, not ignored constants."""

    def test_default_page_size_comes_from_settings(self):
        page = pagination(page=1, page_size=None, settings=_settings(default_page_size=10))
        assert page.page_size == 10

    def test_explicit_page_size_within_max_is_used(self):
        page = pagination(page=1, page_size=50, settings=_settings(max_page_size=200))
        assert page.page_size == 50

    def test_page_size_over_configured_max_is_rejected(self):
        with pytest.raises(ValidationError):
            pagination(page=1, page_size=201, settings=_settings(max_page_size=200))
