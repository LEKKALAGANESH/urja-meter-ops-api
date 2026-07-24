"""End-to-end tests through the whole stack, with only the portal replaced.

Every other test in this suite substitutes something *inside* the service: `test_api.py`
injects a fake snapshot store, `test_portal_client.py` exercises the adapter in isolation.
That leaves one seam uncovered — the path where a real HTTP request enters the app, flows
through routing, dependency wiring, the snapshot store, the portal adapter, session
handling and normalisation, and comes back as JSON.

These tests mock **only the portal's HTTP responses** (via respx) and drive the real
application through `TestClient`. Nothing else is faked, so they catch integration faults
that unit tests structurally cannot: dependency wiring, lifespan ordering, error mapping
across layer boundaries, and cache behaviour under real request flow.

Portal payloads are the recorded fixtures used elsewhere, so the shapes are real.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

from .conftest import load_fixture

PORTAL = "https://portal.e2e.test"
COOKIE = "__Secure-better-auth.session_token=e2e-token; Path=/"


def portal_routes(respx_mock, *, meters=None, dts=None, energy=None):
    """Stand up a complete fake portal at the HTTP boundary."""
    respx_mock.post(f"{PORTAL}/api/auth/sign-in/email").mock(
        return_value=httpx.Response(
            200,
            json={
                "token": "t",
                "user": {},
                "session": {"expiresAt": "2099-01-01T00:00:00.000Z"},
            },
            headers={"set-cookie": COOKIE},
        )
    )
    respx_mock.get(f"{PORTAL}/api/auth/get-session").mock(
        return_value=httpx.Response(200, json={"session": {"token": "t"}, "user": {}})
    )
    respx_mock.get(f"{PORTAL}/portal/keys").mock(
        return_value=httpx.Response(200, json={"data": {"signingSecret": "e2e-secret"}})
    )
    respx_mock.get(f"{PORTAL}/portal/dts").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": dts if dts is not None else load_fixture("dts_page1.json")["data"],
                "total": len(dts) if dts is not None else 8,
            },
        )
    )
    rows = meters if meters is not None else load_fixture("export_sample.json")["data"]
    respx_mock.get(f"{PORTAL}/portal/export").mock(
        return_value=httpx.Response(200, json={"data": rows, "total": len(rows)})
    )
    respx_mock.get(url__regex=rf"{PORTAL}/portal/meters/.+/energy").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": energy
                if energy is not None
                else load_fixture("energy_30min.json")["data"]
            },
        )
    )
    return respx_mock


@pytest.fixture
def settings() -> Settings:
    return Settings(
        portal_base_url=PORTAL,
        portal_username="e2e@test",
        portal_password="secret",
        snapshot_refresh_on_start=True,
        snapshot_ttl_seconds=300,
        snapshot_refresh_min_interval_seconds=0,  # economics tests below force refreshes
        log_format="console",
    )


class TestColdStartToResponse:
    """Boot the service against the portal and serve a request, end to end."""

    def test_startup_builds_a_snapshot_and_serves_meters(self, respx_mock, settings):
        portal_routes(respx_mock)
        with TestClient(create_app(settings)) as client:
            body = client.get("/api/v1/meters").json()
        assert body["meta"]["total_items"] == 7
        assert body["items"][0]["meter_id"].startswith("J")

    def test_full_pipeline_normalises_upstream_payloads(self, respx_mock, settings):
        """camelCase strings upstream must arrive as typed snake_case fields."""
        portal_routes(respx_mock)
        with TestClient(create_app(settings)) as client:
            meter = client.get("/api/v1/meters/J100000").json()
        assert meter["install_status"] == "decommissioned"
        assert isinstance(meter["location"]["latitude"], float)
        assert [ref["level"] for ref in meter["hierarchy"]][:2] == ["zone", "circle"]

    def test_hierarchy_is_reconstructed_through_the_stack(self, respx_mock, settings):
        portal_routes(respx_mock)
        with TestClient(create_app(settings)) as client:
            tree = client.get("/api/v1/hierarchy").json()
        assert tree["id"] == "network"
        assert tree["meter_count"] == 7
        assert tree["children"]

    def test_consumption_is_derived_from_live_portal_readings(self, respx_mock, settings):
        """Registers are cumulative; the API must return differences, not raw totals."""
        portal_routes(respx_mock)
        with TestClient(create_app(settings)) as client:
            body = client.get("/api/v1/meters/J100001/consumption").json()
        raw = load_fixture("energy_30min.json")["data"]
        expected = round(float(raw[-1]["kwh"]) - float(raw[0]["kwh"]), 3)
        assert body["summary"]["total_kwh"] == pytest.approx(expected, abs=1e-6)

    def test_signed_export_is_actually_signed(self, respx_mock, settings):
        portal_routes(respx_mock)
        with TestClient(create_app(settings)):
            pass
        export_calls = [c for c in respx_mock.calls if "/portal/export" in str(c.request.url)]
        assert export_calls, "export was never called"
        headers = export_calls[0].request.headers
        assert headers["x-signature"] and headers["x-timestamp"]


class TestSessionLifecycleThroughTheStack:
    def test_expired_session_is_recovered_without_the_caller_noticing(
        self, respx_mock, settings
    ):
        """The portal 401s once mid-flight; the caller must still get a 200."""
        portal_routes(respx_mock)
        respx_mock.get(url__regex=rf"{PORTAL}/portal/meters/.+/energy").mock(
            side_effect=[
                httpx.Response(401, json={"error": "unauthorized"}),
                httpx.Response(200, json={"data": load_fixture("energy_30min.json")["data"]}),
            ]
        )
        with TestClient(create_app(settings)) as client:
            response = client.get("/api/v1/meters/J100001/consumption")
        assert response.status_code == 200
        logins = [c for c in respx_mock.calls if "sign-in" in str(c.request.url)]
        assert len(logins) >= 2, "should have re-authenticated"

    def test_readiness_reflects_a_genuinely_valid_session(self, respx_mock, settings):
        portal_routes(respx_mock)
        with TestClient(create_app(settings)) as client:
            body = client.get("/api/v1/health/ready").json()
        assert body["status"] == "ready"
        assert body["checks"]["portal_session"]["ok"] is True


class TestUpstreamFailurePropagation:
    """Portal faults must reach the caller as the documented contract, never a raw 500."""

    def test_portal_outage_during_consumption_becomes_503(self, respx_mock, settings):
        portal_routes(respx_mock)
        with TestClient(create_app(settings)) as client:
            respx_mock.get(url__regex=rf"{PORTAL}/portal/meters/.+/energy").mock(
                return_value=httpx.Response(503)
            )
            response = client.get("/api/v1/meters/J100001/consumption")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "upstream_unavailable"

    def test_portal_timeout_becomes_504(self, respx_mock, settings):
        portal_routes(respx_mock)
        with TestClient(create_app(settings)) as client:
            respx_mock.get(url__regex=rf"{PORTAL}/portal/meters/.+/energy").mock(
                side_effect=httpx.ReadTimeout("slow")
            )
            response = client.get("/api/v1/meters/J100001/consumption")
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "upstream_timeout"

    def test_upstream_html_becomes_502_not_a_crash(self, respx_mock, settings):
        portal_routes(respx_mock)
        with TestClient(create_app(settings)) as client:
            respx_mock.get(url__regex=rf"{PORTAL}/portal/meters/.+/energy").mock(
                return_value=httpx.Response(
                    200, text="<!doctype html>", headers={"content-type": "text/html"}
                )
            )
            response = client.get("/api/v1/meters/J100001/consumption")
        assert response.status_code == 502
        assert "10." not in response.json()["error"]["message"]

    def test_every_error_carries_a_correlation_id(self, respx_mock, settings):
        portal_routes(respx_mock)
        with TestClient(create_app(settings)) as client:
            respx_mock.get(url__regex=rf"{PORTAL}/portal/meters/.+/energy").mock(
                return_value=httpx.Response(503)
            )
            response = client.get("/api/v1/meters/J100001/consumption")
        assert response.json()["error"]["request_id"] == response.headers["x-request-id"]

    def test_service_starts_even_when_the_portal_is_down(self, respx_mock, settings):
        """A portal outage must not crash-loop the container."""
        # A down portal fails every route, not just sign-in - including the readiness probe.
        respx_mock.post(f"{PORTAL}/api/auth/sign-in/email").mock(
            side_effect=httpx.ConnectError("portal down")
        )
        respx_mock.get(f"{PORTAL}/api/auth/get-session").mock(
            side_effect=httpx.ConnectError("portal down")
        )
        with TestClient(create_app(settings)) as client:
            assert client.get("/api/v1/health/live").status_code == 200
            assert client.get("/api/v1/health/ready").status_code == 503


class TestSnapshotEconomicsUnderRealRequestFlow:
    def test_many_requests_cost_one_upstream_export(self, respx_mock, settings):
        """The whole point of the snapshot: request volume is decoupled from portal load."""
        portal_routes(respx_mock)
        with TestClient(create_app(settings)) as client:
            for _ in range(12):
                assert client.get("/api/v1/meters").status_code == 200
        exports = [c for c in respx_mock.calls if "/portal/export" in str(c.request.url)]
        assert len(exports) == 1

    def test_forced_refresh_costs_exactly_one_more_export(self, respx_mock, settings):
        portal_routes(respx_mock)
        with TestClient(create_app(settings)) as client:
            client.get("/api/v1/meters")
            client.post("/api/v1/system/snapshot/refresh")
        exports = [c for c in respx_mock.calls if "/portal/export" in str(c.request.url)]
        assert len(exports) == 2

    def test_refresh_burst_is_throttled_to_protect_the_portal(self, respx_mock, settings):
        """The unauthenticated refresh endpoint must not amplify load on the portal.

        With a live interval, a burst of forced refreshes coalesces: the first rebuilds,
        the rest get 429 + Retry-After and never reach the portal - one export, not many.
        """
        portal_routes(respx_mock)
        throttled = settings.model_copy(update={"snapshot_refresh_min_interval_seconds": 300})
        with TestClient(create_app(throttled)) as client:
            statuses = [
                client.post("/api/v1/system/snapshot/refresh").status_code for _ in range(5)
            ]
            coalesced = client.post("/api/v1/system/snapshot/refresh")
        assert statuses[0] == 200
        assert statuses[1:] == [429, 429, 429, 429]
        assert coalesced.json()["error"]["code"] == "refresh_throttled"
        assert coalesced.headers["retry-after"] == "300"
        exports = [c for c in respx_mock.calls if "/portal/export" in str(c.request.url)]
        assert len(exports) == 2  # one on startup, one for the first forced refresh

    def test_repeat_consumption_reads_hit_the_cache(self, respx_mock, settings):
        portal_routes(respx_mock)
        with TestClient(create_app(settings)) as client:
            for _ in range(4):
                client.get("/api/v1/meters/J100001/consumption")
        series = [c for c in respx_mock.calls if "/energy" in str(c.request.url)]
        assert len(series) == 1

    def test_degraded_fallback_when_export_is_unavailable_on_cold_start(
        self, respx_mock, settings
    ):
        """No snapshot yet: a poorer answer beats no answer, and is labelled as such."""
        portal_routes(respx_mock)
        respx_mock.get(f"{PORTAL}/portal/export").mock(return_value=httpx.Response(500))
        respx_mock.get(f"{PORTAL}/portal/meters/search").mock(
            return_value=httpx.Response(
                200, json={"data": load_fixture("export_sample.json")["data"], "total": 7}
            )
        )
        with TestClient(create_app(settings)) as client:
            info = client.get("/api/v1/system/snapshot").json()
            meters = client.get("/api/v1/meters").json()
        assert info["source"] == "search"
        assert meters["meta"]["total_items"] == 7
