"""Contract tests against the **live** Urja portal.

Deselected by default (``-m "not live"``); run with credentials via ``make test-live``.

Purpose: every claim in ``PROTOCOL.md`` is an empirical observation, and observations rot.
These tests re-verify the claims the implementation depends on, so if the portal changes,
the failure is a named assertion rather than a mysterious production bug.

They are read-only - GETs plus the login the portal requires - and deliberately few, to
stay a well-behaved client.
"""

from __future__ import annotations

import itertools

import httpx
import pytest
import pytest_asyncio

from app.config import Settings
from app.domain import normalize
from app.portal.client import UrjaPortalClient
from app.portal.session import PortalSession
from app.portal.signing import MAX_CLOCK_SKEW_SECONDS, sign_request

# One session for the whole module. The login route is rate limited (3 per window), so a
# fresh login per test would throttle us - and hammering a third party's auth endpoint is
# not how a well-behaved client behaves regardless.
pytestmark = [pytest.mark.live, pytest.mark.asyncio(loop_scope="module")]


@pytest.fixture(scope="module")
def settings() -> Settings:
    config = Settings()
    if not config.has_credentials:
        pytest.skip("PORTAL_USERNAME/PORTAL_PASSWORD not set")
    return config


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def client(settings: Settings):
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as http:
        session = PortalSession(
            http,
            settings.portal_base_url,
            settings.portal_username,
            settings.portal_password.get_secret_value(),
        )
        yield UrjaPortalClient(http, session, settings.portal_base_url)


class TestAuthenticationContract:
    async def test_login_issues_a_session_cookie(self, client, settings):
        await client._session.ensure_authenticated()
        assert client._session.is_authenticated

    async def test_session_advertises_an_expiry(self, client):
        await client._session.ensure_authenticated()
        assert client._session.expires_at is not None

    async def test_unauthenticated_portal_call_returns_401_json(self, settings):
        """The signal our reactive re-authentication depends on."""
        async with httpx.AsyncClient(timeout=30.0) as http:
            response = await http.get(f"{settings.portal_base_url}/portal/dts?page=1")
        assert response.status_code == 401
        assert response.json()["error"] == "unauthorized"


class TestEndpointContract:
    async def test_search_returns_data_and_total(self, client):
        rows, total = await client.search_meters(page=1)
        assert total > 0 and len(rows) <= 20
        assert {"meterId", "serialNo", "make"} <= set(rows[0])

    async def test_dt_registry_is_complete(self, client):
        rows = await client.list_all_dts()
        _, total = await client.list_dts(page=1)
        assert len(rows) == total

    async def test_energy_series_shape(self, client):
        rows = await client.get_energy_series("J100001")
        assert rows and {"timestamp", "kwh", "kvah", "voltR"} <= set(rows[0])

    async def test_energy_values_are_strings_upstream(self, client):
        """Justifies the numeric coercion in the normaliser."""
        rows = await client.get_energy_series("J100001")
        assert isinstance(rows[0]["kwh"], str)

    async def test_energy_ignores_range_parameters(self, client):
        """Why windowing is implemented locally rather than delegated upstream."""
        baseline = await client.get_energy_series("J100001")
        response = await client._http.get(
            f"{client._base_url}/portal/meters/J100001/energy",
            params={"from": "24/06/2026", "to": "25/06/2026"},
        )
        assert response.json()["data"] == baseline

    async def test_geo_returns_string_coordinates(self, client):
        geo = await client.get_geo("J100001")
        assert isinstance(geo["latitude"], str)
        assert normalize.geo_from_endpoint(geo) is not None

    async def test_unknown_meter_is_404(self, client):
        from app.portal.exceptions import PortalNotFound

        with pytest.raises(PortalNotFound):
            await client.get_energy_series("DEFINITELY-NOT-A-METER")


class TestSignedExportContract:
    async def test_export_returns_the_full_estate_in_one_call(self, client):
        rows = await client.export_all_meters()
        _, search_total = await client.search_meters(page=1)
        assert len(rows) == search_total

    async def test_export_records_carry_hierarchy_and_geo(self, client):
        rows = await client.export_all_meters()
        assert {"hierarchy", "geo", "build", "installType"} <= set(rows[0])

    async def test_unsigned_export_is_rejected(self, client):
        await client._session.ensure_authenticated()
        response = await client._http.get(
            f"{client._base_url}/portal/export", params={"page": 1}
        )
        assert response.status_code == 401
        assert response.json()["error"] == "signature_invalid"

    async def test_signature_beyond_the_skew_window_is_rejected(self, client):
        import time

        await client._session.ensure_authenticated()
        secret = await client.get_signing_secret()
        stale = int(time.time()) - (MAX_CLOCK_SKEW_SECONDS + 60)
        signed = sign_request(secret, "GET", "/portal/export", "page=1", timestamp=stale)
        response = await client._http.get(
            f"{client._base_url}/portal/export",
            params={"page": 1},
            headers=signed.as_headers(),
        )
        assert response.status_code == 401


class TestDataAssumptions:
    async def test_registers_are_cumulative(self, client):
        """The assumption the whole consumption module rests on."""
        rows = await client.get_energy_series("J100001")
        values = [float(r["kwh"]) for r in rows]
        assert all(b >= a for a, b in itertools.pairwise(values))

    async def test_both_meter_builds_still_exist(self, client):
        rows = await client.export_all_meters()
        assert {r["build"] for r in rows} == {"legacy", "v2"}

    async def test_search_still_treats_underscore_as_a_wildcard(self, client):
        """The quirk that forces local search instead of delegating to the portal."""
        _, wildcard_total = await client.search_meters(query="_", page=1)
        _, empty_total = await client.search_meters(query="", page=1)
        assert wildcard_total == empty_total

    async def test_hierarchy_codes_are_not_globally_unique(self, client):
        """The finding that forces path-based node identity."""
        from collections import defaultdict

        rows = await client.export_all_meters()
        parents = defaultdict(set)
        for row in rows:
            hierarchy = row.get("hierarchy") or {}
            division = (hierarchy.get("division") or {}).get("code")
            circle = (hierarchy.get("circle") or {}).get("code")
            if division and circle:
                parents[division].add(circle)
        assert any(len(circles) > 1 for circles in parents.values())
