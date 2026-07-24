"""Tests for the operations console served at /app.

The console is static, so these verify what the *service* is responsible for: that the
assets are mounted, that the security policy is scoped correctly, and that the page does
not depend on anything the CSP forbids. Behaviour inside the browser is verified
separately by driving a real browser (see README, *Console verification*).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


@pytest.fixture
def client():
    settings = Settings(
        portal_username="test", portal_password="test", snapshot_refresh_on_start=False
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


class TestConsoleIsServed:
    def test_index_is_served_at_app_root(self, client):
        response = client.get("/app/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    @pytest.mark.parametrize(
        "asset,content_type",
        [
            ("styles.css", "text/css"),
            ("app.js", "javascript"),
            ("api.js", "javascript"),
            ("charts.js", "javascript"),
        ],
    )
    def test_assets_are_served_with_correct_types(self, client, asset, content_type):
        response = client.get(f"/app/{asset}")
        assert response.status_code == 200
        assert content_type in response.headers["content-type"]

    def test_root_advertises_the_console(self, client):
        assert client.get("/").json()["console"] == "/app/"

    def test_root_redirects_a_browser_to_the_console(self, client):
        """Opening the base URL in a browser should land on the console, not raw JSON."""
        response = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
        assert response.status_code in (307, 308)
        assert response.headers["location"] == "/app/"

    def test_root_still_serves_the_json_index_to_api_clients(self, client):
        response = client.get("/", headers={"accept": "application/json"})
        assert response.status_code == 200
        assert response.json()["console"] == "/app/"

    def test_favicon_is_served_so_browser_requests_never_404(self, client):
        response = client.get("/favicon.ico")
        assert response.status_code == 200
        assert "image/svg+xml" in response.headers["content-type"]
        assert "<svg" in response.text


class TestContentSecurityPolicyScoping:
    def test_console_policy_allows_its_own_assets(self, client):
        policy = client.get("/app/").headers["content-security-policy"]
        assert "default-src 'self'" in policy
        assert "script-src 'self'" in policy

    def test_console_policy_forbids_inline_and_remote_script(self, client):
        """The strict policy is the point; relaxing it would defeat the exercise."""
        policy = client.get("/app/").headers["content-security-policy"]
        assert "unsafe-inline" not in policy
        assert "unsafe-eval" not in policy
        assert "frame-ancestors 'none'" in policy

    def test_api_keeps_the_stricter_policy(self, client):
        """JSON endpoints load nothing, so they must not inherit the console's relaxation."""
        policy = client.get("/api/v1/health/live").headers["content-security-policy"]
        assert policy.startswith("default-src 'none'")

    def test_swagger_is_exempt(self, client):
        """Swagger injects inline script/style; a policy strict enough to matter breaks it."""
        assert "content-security-policy" not in client.get("/docs").headers


class TestConsoleHonoursItsOwnPolicy:
    """Static guards for the CSP violations that are easy to reintroduce."""

    def test_no_inline_script_or_handlers_in_markup(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\ssrc=)", html), (
            "inline <script> violates script-src"
        )
        assert not re.search(r"\son[a-z]+\s*=", html), (
            "inline event handler violates script-src"
        )

    def test_no_inline_style_blocks_or_attributes_in_markup(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        assert "<style" not in html
        assert not re.search(r"\sstyle\s*=", html), "style attribute violates style-src"

    def test_scripts_never_use_setattribute_for_style(self):
        """setAttribute('style', ...) is blocked by style-src; CSSOM assignment is not.

        Comments are stripped first: this asserts on code, and the helper that does the
        CSSOM assignment legitimately names the pattern it exists to avoid.
        """
        for name in ("app.js", "charts.js", "api.js"):
            source = (STATIC / name).read_text(encoding="utf-8")
            code = "\n".join(
                line.split("//")[0]
                for line in source.splitlines()
                if not line.strip().startswith("*")
            )
            assert "setAttribute('style'" not in code
            assert 'setAttribute("style"' not in code

    def test_no_external_origins_are_referenced(self):
        """A CDN reference would be blocked at runtime and add a third-party dependency."""
        for path in STATIC.iterdir():
            text = path.read_text(encoding="utf-8")
            for origin in ("//cdn.", "//unpkg.", "//cdnjs.", "jsdelivr", "fonts.googleapis"):
                assert origin not in text, f"{path.name} references {origin}"

    def test_page_declares_language_and_viewport(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        assert 'lang="en"' in html
        assert 'name="viewport"' in html
        assert "user-scalable=no" not in html, "must never block user zoom"

    def test_noscript_fallback_points_at_the_api(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        assert "<noscript>" in html
        assert "/openapi.json" in html


class TestShareableFilterState:
    """The meters view claims filter state is reflected in the URL. Guard that the wiring
    that makes it true actually exists, so the claim can never rot back to a dead comment.
    Real browser behaviour (share + reload) is verified separately - see README."""

    def test_state_is_read_from_and_written_to_the_url(self):
        code = (STATIC / "app.js").read_text(encoding="utf-8")
        assert "readMetersStateFromUrl" in code, "URL state must be parsed on entry"
        assert "syncMetersStateToUrl" in code, "URL state must be reflected on change"
        # replaceState, not a hash write: reflecting must not re-route and tear down the view.
        assert "history.replaceState" in code
        assert "URLSearchParams" in code

    def test_url_reflection_is_wired_into_the_load_path(self):
        code = (STATIC / "app.js").read_text(encoding="utf-8")
        load_body = code.split("function loadMeters()", 1)[1].split("function ", 1)[0]
        assert "syncMetersStateToUrl()" in load_body, (
            "every filter/sort/page change routes through loadMeters, so the URL sync "
            "belongs there or shared links silently drift from the visible state"
        )
