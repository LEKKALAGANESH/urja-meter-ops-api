"""Tests for the portal adapter's resilience behaviour, using a mocked transport.

These simulate the portal, so they are only meaningful because the behaviours they
simulate were **observed on the real portal first** and are documented in PROTOCOL.md.
Each test names the live observation it encodes.
"""

from __future__ import annotations

import httpx
import pytest

from app.portal.client import UrjaPortalClient, escape_like
from app.portal.exceptions import (
    PortalNotFound,
    PortalProtocolError,
    PortalSessionExpired,
    PortalTimeout,
    PortalUnavailable,
)
from app.portal.session import PortalSession

BASE = "https://portal.test"
LOGIN_BODY = {
    "token": "t",
    "user": {"email": "operator@urja.local"},
    "session": {"expiresAt": "2099-01-01T00:00:00.000Z"},
}


@pytest.fixture
def mock_login(respx_mock):
    return respx_mock.post(f"{BASE}/api/auth/sign-in/email").mock(
        return_value=httpx.Response(
            200,
            json=LOGIN_BODY,
            headers={"set-cookie": "__Secure-better-auth.session_token=abc; Path=/"},
        )
    )


@pytest.fixture
async def client():
    async with httpx.AsyncClient(follow_redirects=False) as http:
        session = PortalSession(http, BASE, "user", "pass")
        yield UrjaPortalClient(http, session, BASE, max_retries=2, backoff_base=0.0)


class TestLikeEscaping:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("%", r"\%"),
            ("_", r"\_"),
            ("SE%", r"SE\%"),
            ("a_b%c", r"a\_b\%c"),
            ("plain", "plain"),
            ("\\", "\\\\"),
        ],
    )
    def test_wildcards_are_escaped(self, raw, expected):
        """Verified upstream: `q=_` matches all 403 meters, `q=SE%` matches 80."""
        assert escape_like(raw) == expected


class TestSessionExpiryDetection:
    """The portal signals a dead session three different ways."""

    async def test_401_json_triggers_reauthentication(self, respx_mock, mock_login, client):
        route = respx_mock.get(f"{BASE}/portal/dts").mock(
            side_effect=[
                httpx.Response(
                    401,
                    json={"error": "unauthorized", "message": "A valid session is required."},
                ),
                httpx.Response(200, json={"data": [{"code": "DT-001"}], "total": 1}),
            ]
        )
        _rows, total = await client.list_dts()
        assert total == 1
        assert route.call_count == 2
        assert mock_login.call_count == 2  # initial login + re-auth

    async def test_302_to_login_triggers_reauthentication(
        self, respx_mock, mock_login, client
    ):
        respx_mock.get(f"{BASE}/meters/J1/__data.json").mock(
            side_effect=[
                httpx.Response(302, headers={"location": "/login"}),
                httpx.Response(
                    200,
                    json={
                        "type": "data",
                        "nodes": [
                            {"type": "data", "data": [{"detail": 1, "hierarchy": 2}, {}, {}]}
                        ],
                    },
                ),
            ]
        )
        assert await client.get_meter_detail("J1") == {"detail": {}, "hierarchy": {}}

    async def test_200_soft_redirect_is_not_mistaken_for_success(
        self, respx_mock, mock_login, client
    ):
        """__data.json returns HTTP 200 with a redirect body when the session dies."""
        respx_mock.get(f"{BASE}/meters/J1/__data.json").mock(
            return_value=httpx.Response(200, json={"type": "redirect", "location": "/login"})
        )
        with pytest.raises(PortalSessionExpired):
            await client.get_meter_detail("J1")

    async def test_repeated_rejection_stops_rather_than_looping(
        self, respx_mock, mock_login, client
    ):
        respx_mock.get(f"{BASE}/portal/dts").mock(
            return_value=httpx.Response(401, json={"error": "unauthorized"})
        )
        with pytest.raises(PortalSessionExpired):
            await client.list_dts()


class TestRetryBehaviour:
    async def test_transient_5xx_is_retried(self, respx_mock, mock_login, client):
        route = respx_mock.get(f"{BASE}/portal/dts").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json={"data": [], "total": 0}),
            ]
        )
        await client.list_dts()
        assert route.call_count == 2

    async def test_persistent_5xx_gives_up_with_a_typed_error(
        self, respx_mock, mock_login, client
    ):
        respx_mock.get(f"{BASE}/portal/dts").mock(return_value=httpx.Response(503))
        with pytest.raises(PortalUnavailable):
            await client.list_dts()

    async def test_timeout_is_retried_then_surfaced(self, respx_mock, mock_login, client):
        respx_mock.get(f"{BASE}/portal/dts").mock(side_effect=httpx.ReadTimeout("too slow"))
        with pytest.raises(PortalTimeout):
            await client.list_dts()

    async def test_404_is_not_retried(self, respx_mock, mock_login, client):
        """A 404 is deterministic; retrying only wastes the portal's time."""
        route = respx_mock.get(f"{BASE}/portal/meters/NOPE/energy").mock(
            return_value=httpx.Response(404, json={"error": "not_found"})
        )
        with pytest.raises(PortalNotFound):
            await client.get_energy_series("NOPE")
        assert route.call_count == 1


class TestPayloadValidation:
    async def test_html_on_a_json_route_is_a_protocol_error(
        self, respx_mock, mock_login, client
    ):
        """An HTML body means we were bounced somewhere unexpected - do not parse hopefully."""
        respx_mock.get(f"{BASE}/portal/dts").mock(
            return_value=httpx.Response(
                200, text="<!doctype html>", headers={"content-type": "text/html"}
            )
        )
        with pytest.raises(PortalProtocolError):
            await client.list_dts()

    async def test_missing_total_degrades_instead_of_failing(
        self, respx_mock, mock_login, client
    ):
        respx_mock.get(f"{BASE}/portal/dts").mock(
            return_value=httpx.Response(200, json={"data": [{"code": "DT-001"}]})
        )
        _rows, total = await client.list_dts()
        assert total == 1

    async def test_non_dict_rows_are_filtered_out(self, respx_mock, mock_login, client):
        respx_mock.get(f"{BASE}/portal/dts").mock(
            return_value=httpx.Response(
                200, json={"data": [{"code": "DT-001"}, "junk"], "total": 2}
            )
        )
        rows, _ = await client.list_dts()
        assert rows == [{"code": "DT-001"}]

    async def test_unknown_meter_detail_maps_to_not_found(
        self, respx_mock, mock_login, client
    ):
        """The portal returns HTTP 200 with an embedded 404 error node."""
        respx_mock.get(f"{BASE}/meters/NOPE/__data.json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "type": "data",
                    "nodes": [
                        {
                            "type": "error",
                            "error": {"message": "Meter not found"},
                            "status": 404,
                        }
                    ],
                },
            )
        )
        with pytest.raises(PortalNotFound):
            await client.get_meter_detail("NOPE")


class TestSignedExport:
    async def test_export_sends_signature_headers(self, respx_mock, mock_login, client):
        respx_mock.get(f"{BASE}/portal/keys").mock(
            return_value=httpx.Response(200, json={"data": {"signingSecret": "s3cret"}})
        )
        route = respx_mock.get(f"{BASE}/portal/export").mock(
            return_value=httpx.Response(200, json={"data": [{"meterId": "J1"}], "total": 1})
        )
        rows = await client.export_all_meters()
        assert rows == [{"meterId": "J1"}]
        request = route.calls[0].request
        assert request.headers["x-signature"] and request.headers["x-timestamp"]

    async def test_signing_secret_is_cached(self, respx_mock, mock_login, client):
        keys = respx_mock.get(f"{BASE}/portal/keys").mock(
            return_value=httpx.Response(200, json={"data": {"signingSecret": "s"}})
        )
        respx_mock.get(f"{BASE}/portal/export").mock(
            return_value=httpx.Response(200, json={"data": [], "total": 0})
        )
        await client.export_all_meters()
        await client.export_all_meters()
        assert keys.call_count == 1

    async def test_rejected_signature_refetches_the_secret_once(
        self, respx_mock, mock_login, client
    ):
        keys = respx_mock.get(f"{BASE}/portal/keys").mock(
            return_value=httpx.Response(200, json={"data": {"signingSecret": "s"}})
        )
        respx_mock.get(f"{BASE}/portal/export").mock(
            side_effect=[
                httpx.Response(401, json={"error": "signature_invalid"}),
                httpx.Response(200, json={"data": [], "total": 0}),
            ]
        )
        await client.export_all_meters()
        assert keys.call_count == 2

    async def test_missing_secret_is_a_protocol_error(self, respx_mock, mock_login, client):
        respx_mock.get(f"{BASE}/portal/keys").mock(
            return_value=httpx.Response(200, json={"data": {}})
        )
        with pytest.raises(PortalProtocolError):
            await client.get_signing_secret()


class TestPagination:
    async def test_all_dts_walks_every_page(self, respx_mock, mock_login, client):
        respx_mock.get(f"{BASE}/portal/dts", params={"page": "1"}).mock(
            return_value=httpx.Response(
                200, json={"data": [{"code": f"DT-{i:03d}"} for i in range(20)], "total": 25}
            )
        )
        respx_mock.get(f"{BASE}/portal/dts", params={"page": "2"}).mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"code": f"DT-{i:03d}"} for i in range(20, 25)], "total": 25},
            )
        )
        assert len(await client.list_all_dts()) == 25

    async def test_pagination_stops_if_total_lies(self, respx_mock, mock_login, client):
        """Guards against an infinite loop if `total` disagrees with the pages served."""
        respx_mock.get(f"{BASE}/portal/dts", params={"page": "1"}).mock(
            return_value=httpx.Response(200, json={"data": [{"code": "DT-001"}], "total": 999})
        )
        respx_mock.get(f"{BASE}/portal/dts", params={"page": "2"}).mock(
            return_value=httpx.Response(200, json={"data": [], "total": 999})
        )
        assert len(await client.list_all_dts()) == 1
