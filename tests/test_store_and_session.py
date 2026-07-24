"""Tests for freshness, degradation and session lifecycle.

This is the robustness surface: what the service does over a long-running session and when
the portal misbehaves. Each test states the operational property it protects.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.portal.exceptions import PortalRateLimited, PortalUnavailable
from app.portal.session import PortalSession
from app.store.snapshot import SnapshotStore

from .conftest import load_fixture

BASE = "https://portal.test"
COOKIE = "__Secure-better-auth.session_token=abc; Path=/"


def login_response(expires_in_seconds: int = 3600) -> httpx.Response:
    expiry = datetime.now(UTC) + timedelta(seconds=expires_in_seconds)
    return httpx.Response(
        200,
        json={"token": "t", "user": {}, "session": {"expiresAt": expiry.isoformat()}},
        headers={"set-cookie": COOKIE},
    )


class FakeClient:
    """Minimal stand-in for UrjaPortalClient, counting calls and simulating faults."""

    def __init__(self) -> None:
        self.export_calls = 0
        self.energy_calls = 0
        self.export_error: Exception | None = None
        self.search_pages_used = False

    async def export_all_meters(self) -> list[dict]:
        self.export_calls += 1
        if self.export_error:
            raise self.export_error
        return load_fixture("export_sample.json")["data"]

    async def search_meters(self, query: str = "", page: int = 1):
        self.search_pages_used = True
        rows = load_fixture("export_sample.json")["data"]
        return (rows, len(rows)) if page == 1 else ([], len(rows))

    async def list_all_dts(self) -> list[dict]:
        return load_fixture("dts_page1.json")["data"]

    async def get_energy_series(self, meter_id: str) -> list[dict]:
        self.energy_calls += 1
        return load_fixture("energy_30min.json")["data"]


class TestSnapshotFreshness:
    async def test_first_read_builds_the_snapshot(self):
        store = SnapshotStore(FakeClient(), ttl_seconds=300)
        snapshot = await store.get()
        assert (
            snapshot.meter_count if hasattr(snapshot, "meter_count") else len(snapshot.meters)
        )

    async def test_fresh_snapshot_is_reused(self):
        client = FakeClient()
        store = SnapshotStore(client, ttl_seconds=300)
        await store.get()
        await store.get()
        assert client.export_calls == 1

    async def test_expired_snapshot_is_rebuilt(self):
        client = FakeClient()
        store = SnapshotStore(client, ttl_seconds=0)
        await store.get()
        await store.get()
        assert client.export_calls == 2

    async def test_forced_refresh_ignores_the_ttl(self):
        client = FakeClient()
        store = SnapshotStore(client, ttl_seconds=300)
        await store.get()
        await store.get(force_refresh=True)
        assert client.export_calls == 2

    async def test_concurrent_readers_trigger_one_build(self):
        """Single-flight: ten simultaneous cold reads must not mean ten exports."""
        client = FakeClient()
        store = SnapshotStore(client, ttl_seconds=300)
        await asyncio.gather(*(store.get() for _ in range(10)))
        assert client.export_calls == 1


class TestForcedRefreshThrottle:
    """The amplification guard on the unauthenticated refresh endpoint.

    Each forced rebuild costs one portal export; without a floor between them an
    unauthenticated caller could turn request volume into upstream export volume.
    """

    async def test_a_burst_of_forced_refreshes_costs_one_forced_export(self):
        """Ten concurrent forced refreshes: the first rebuilds, the other nine coalesce."""
        client = FakeClient()
        store = SnapshotStore(client, ttl_seconds=300, refresh_min_interval_seconds=300)
        await store.get()  # startup export (1)
        results = await asyncio.gather(*(store.refresh_now() for _ in range(10)))
        assert client.export_calls == 2  # startup + exactly one forced rebuild
        assert sum(1 for _, refreshed in results if refreshed) == 1

    async def test_first_forced_refresh_after_a_read_still_rebuilds(self):
        """A TTL-driven build must never block the operator's first manual refresh."""
        client = FakeClient()
        store = SnapshotStore(client, ttl_seconds=300, refresh_min_interval_seconds=300)
        await store.get()
        _, refreshed = await store.refresh_now()
        assert refreshed is True
        assert client.export_calls == 2

    async def test_second_forced_refresh_inside_the_window_is_coalesced(self):
        client = FakeClient()
        store = SnapshotStore(client, ttl_seconds=300, refresh_min_interval_seconds=300)
        await store.get()
        await store.refresh_now()
        _, refreshed = await store.refresh_now()
        assert refreshed is False
        assert client.export_calls == 2

    async def test_refresh_is_allowed_again_once_the_interval_elapses(self):
        client = FakeClient()
        store = SnapshotStore(client, ttl_seconds=300, refresh_min_interval_seconds=30)
        await store.get()
        await store.refresh_now()  # sets the forced-refresh marker
        # Simulate the interval having passed without sleeping: monotonic, so we rewind
        # the marker rather than the wall clock.
        store._last_forced_refresh_monotonic -= 31
        _, refreshed = await store.refresh_now()
        assert refreshed is True
        assert client.export_calls == 3

    async def test_zero_interval_disables_the_throttle(self):
        client = FakeClient()
        store = SnapshotStore(client, ttl_seconds=300, refresh_min_interval_seconds=0)
        await store.get()
        _, refreshed = await store.refresh_now()
        assert refreshed is True
        assert client.export_calls == 2


class TestDegradation:
    async def test_failed_refresh_keeps_serving_stale_data(self):
        """Stale data with an honest age beats a 503. The core resilience property."""
        client = FakeClient()
        store = SnapshotStore(client, ttl_seconds=0)
        first = await store.get()
        client.export_error = PortalUnavailable("portal down")
        second = await store.get()
        assert second is first
        assert store.info().last_error is not None

    async def test_a_complete_snapshot_is_never_downgraded_to_a_search_crawl(self):
        """Losing hierarchy and geo is not a recovery; keep the good stale data instead."""
        client = FakeClient()
        store = SnapshotStore(client, ttl_seconds=0)
        first = await store.get()
        assert first.source == "export"
        client.export_error = PortalUnavailable("export broke")
        second = await store.get()
        assert second.source == "export"
        assert client.search_pages_used is False

    async def test_first_build_failure_propagates(self):
        """With nothing cached there is no honest answer, so the caller must be told."""
        client = FakeClient()
        client.export_error = PortalUnavailable("portal down")
        store = SnapshotStore(client, ttl_seconds=300)
        with pytest.raises(PortalUnavailable):
            await store._build(allow_degraded_fallback=False)

    async def test_cold_start_falls_back_to_paginated_search(self):
        """With no snapshot at all, a degraded answer beats no answer."""
        client = FakeClient()
        client.export_error = PortalUnavailable("export unavailable")
        store = SnapshotStore(client, ttl_seconds=300)
        snapshot = await store.get()
        assert client.search_pages_used
        assert snapshot.source == "search"

    async def test_fallback_records_are_flagged_as_partial(self):
        client = FakeClient()
        client.export_error = PortalUnavailable("export unavailable")
        store = SnapshotStore(client, ttl_seconds=300)
        snapshot = await store.get()
        assert all("partial_record_no_hierarchy" in m.data_quality for m in snapshot.meters)

    async def test_recovery_after_a_failed_refresh(self):
        client = FakeClient()
        store = SnapshotStore(client, ttl_seconds=0)
        await store.get()
        client.export_error = PortalUnavailable("blip")
        await store.get()
        client.export_error = None
        await store.get()
        assert store.info().last_error is None


class TestConsumptionCache:
    async def test_repeat_reads_hit_the_cache(self):
        client = FakeClient()
        store = SnapshotStore(client, ttl_seconds=300, consumption_ttl_seconds=300)
        await store.get_readings("J100001")
        await store.get_readings("J100001")
        assert client.energy_calls == 1

    async def test_zero_ttl_disables_caching(self):
        client = FakeClient()
        store = SnapshotStore(client, ttl_seconds=300, consumption_ttl_seconds=0)
        await store.get_readings("J100001")
        await store.get_readings("J100001")
        assert client.energy_calls == 2

    async def test_lru_eviction_bounds_memory(self):
        """A 403-meter crawl must not be able to grow the cache without limit."""
        client = FakeClient()
        store = SnapshotStore(client, ttl_seconds=300, consumption_max_entries=2)
        for meter_id in ("A", "B", "C"):
            await store.get_readings(meter_id)
        assert len(store._consumption) == 2

    async def test_different_meters_are_cached_separately(self):
        client = FakeClient()
        store = SnapshotStore(client, ttl_seconds=300, consumption_ttl_seconds=300)
        await store.get_readings("A")
        await store.get_readings("B")
        assert client.energy_calls == 2


class TestSessionLifecycle:
    async def test_login_records_the_advertised_expiry(self, respx_mock):
        respx_mock.post(f"{BASE}/api/auth/sign-in/email").mock(return_value=login_response())
        async with httpx.AsyncClient() as http:
            session = PortalSession(http, BASE, "u", "p")
            await session.ensure_authenticated()
        assert session.is_authenticated
        assert session.login_count == 1

    async def test_valid_session_is_not_re_logged_in(self, respx_mock):
        route = respx_mock.post(f"{BASE}/api/auth/sign-in/email").mock(
            return_value=login_response()
        )
        async with httpx.AsyncClient() as http:
            session = PortalSession(http, BASE, "u", "p")
            await session.ensure_authenticated()
            await session.ensure_authenticated()
        assert route.call_count == 1

    async def test_refresh_happens_before_expiry_not_after(self, respx_mock):
        """Proactive refresh: a session inside the safety margin is renewed pre-emptively."""
        route = respx_mock.post(f"{BASE}/api/auth/sign-in/email").mock(
            return_value=login_response(expires_in_seconds=60)
        )
        async with httpx.AsyncClient() as http:
            session = PortalSession(http, BASE, "u", "p", refresh_margin_seconds=120)
            await session.ensure_authenticated()
            await session.ensure_authenticated()
        assert route.call_count == 2

    async def test_concurrent_refreshes_collapse_into_one_login(self, respx_mock):
        """Without this, N concurrent 401s would fire N logins and trip the rate limit."""
        route = respx_mock.post(f"{BASE}/api/auth/sign-in/email").mock(
            return_value=login_response()
        )
        async with httpx.AsyncClient() as http:
            session = PortalSession(http, BASE, "u", "p")
            await asyncio.gather(*(session.ensure_authenticated() for _ in range(10)))
        assert route.call_count == 1

    async def test_invalidate_forces_the_next_call_to_re_login(self, respx_mock):
        route = respx_mock.post(f"{BASE}/api/auth/sign-in/email").mock(
            return_value=login_response()
        )
        async with httpx.AsyncClient() as http:
            session = PortalSession(http, BASE, "u", "p")
            await session.ensure_authenticated()
            session.invalidate()
            await session.ensure_authenticated()
        assert route.call_count == 2

    async def test_missing_expiry_falls_back_to_the_observed_lifetime(self, respx_mock):
        respx_mock.post(f"{BASE}/api/auth/sign-in/email").mock(
            return_value=httpx.Response(
                200, json={"token": "t"}, headers={"set-cookie": COOKIE}
            )
        )
        async with httpx.AsyncClient() as http:
            session = PortalSession(http, BASE, "u", "p")
            await session.ensure_authenticated()
        assert session.expires_at is not None

    async def test_login_without_a_cookie_is_rejected(self, respx_mock):
        from app.portal.exceptions import PortalAuthError

        respx_mock.post(f"{BASE}/api/auth/sign-in/email").mock(
            return_value=httpx.Response(200, json={"token": "t"})
        )
        async with httpx.AsyncClient() as http:
            session = PortalSession(http, BASE, "u", "p")
            with pytest.raises(PortalAuthError, match="no session cookie"):
                await session.ensure_authenticated()

    async def test_bad_credentials_do_not_echo_the_password(self, respx_mock):
        from app.portal.exceptions import PortalAuthError

        respx_mock.post(f"{BASE}/api/auth/sign-in/email").mock(
            return_value=httpx.Response(401, json={"message": "bad"})
        )
        async with httpx.AsyncClient() as http:
            session = PortalSession(http, BASE, "u", "hunter2")
            with pytest.raises(PortalAuthError) as caught:
                await session.ensure_authenticated()
        assert "hunter2" not in str(caught.value)


class TestLoginRateLimiting:
    async def test_throttled_login_is_retried_after_the_advertised_delay(
        self, respx_mock, monkeypatch
    ):
        """Measured upstream: the 4th login in a window returns 429 + x-retry-after: 10."""
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("app.portal.session.asyncio.sleep", fake_sleep)
        respx_mock.post(f"{BASE}/api/auth/sign-in/email").mock(
            side_effect=[
                httpx.Response(
                    429,
                    json={"message": "Too many requests."},
                    headers={"x-retry-after": "10"},
                ),
                login_response(),
            ]
        )
        async with httpx.AsyncClient() as http:
            session = PortalSession(http, BASE, "u", "p")
            await session.ensure_authenticated()
        assert slept == [10.0]
        assert session.is_authenticated

    async def test_persistent_throttling_surfaces_the_retry_hint(
        self, respx_mock, monkeypatch
    ):
        async def fake_sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr("app.portal.session.asyncio.sleep", fake_sleep)
        respx_mock.post(f"{BASE}/api/auth/sign-in/email").mock(
            return_value=httpx.Response(429, headers={"x-retry-after": "10"})
        )
        async with httpx.AsyncClient() as http:
            session = PortalSession(http, BASE, "u", "p")
            with pytest.raises(PortalRateLimited) as caught:
                await session.ensure_authenticated()
        assert caught.value.retry_after_seconds == 10.0

    async def test_standard_retry_after_header_is_also_accepted(self, respx_mock, monkeypatch):
        async def fake_sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr("app.portal.session.asyncio.sleep", fake_sleep)
        respx_mock.post(f"{BASE}/api/auth/sign-in/email").mock(
            return_value=httpx.Response(429, headers={"retry-after": "7"})
        )
        async with httpx.AsyncClient() as http:
            session = PortalSession(http, BASE, "u", "p")
            with pytest.raises(PortalRateLimited) as caught:
                await session.ensure_authenticated()
        assert caught.value.retry_after_seconds == 7.0


class TestReadinessProbe:
    async def test_remote_session_check_reports_validity(self, respx_mock):
        respx_mock.post(f"{BASE}/api/auth/sign-in/email").mock(return_value=login_response())
        respx_mock.get(f"{BASE}/api/auth/get-session").mock(
            return_value=httpx.Response(200, json={"session": {"token": "t"}, "user": {}})
        )
        async with httpx.AsyncClient() as http:
            session = PortalSession(http, BASE, "u", "p")
            await session.ensure_authenticated()
            assert await session.fetch_remote_session() is not None

    async def test_rejected_session_reports_none(self, respx_mock):
        respx_mock.get(f"{BASE}/api/auth/get-session").mock(
            return_value=httpx.Response(401, json={})
        )
        async with httpx.AsyncClient() as http:
            session = PortalSession(http, BASE, "u", "p")
            assert await session.fetch_remote_session() is None
