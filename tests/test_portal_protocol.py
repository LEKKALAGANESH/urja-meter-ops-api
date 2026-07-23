"""Tests for the portal protocol primitives: HMAC signing and devalue decoding.

These encode the reverse-engineered contract. If the portal ever changes how it signs or
encodes, these fail first and point at exactly what moved.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from app.portal.devalue import extract_data_nodes, find_node_with_keys, unflatten
from app.portal.exceptions import PortalProtocolError
from app.portal.signing import MAX_CLOCK_SKEW_SECONDS, build_message, sign_request


class TestSigning:
    def test_message_layout_matches_portal_client(self):
        """The portal's own bundle joins [method, path, query, ts] with newlines."""
        assert build_message("GET", "/portal/export", "page=1", "1700000000") == (
            "GET\n/portal/export\npage=1\n1700000000"
        )

    def test_signature_is_hmac_sha256_hex(self):
        secret, ts = "s3cret", 1700000000
        signed = sign_request(secret, "GET", "/portal/export", "page=1", timestamp=ts)
        expected = hmac.new(
            secret.encode(),
            b"GET\n/portal/export\npage=1\n1700000000",
            hashlib.sha256,
        ).hexdigest()
        assert signed.signature == expected
        assert signed.timestamp == "1700000000"

    def test_signature_is_bound_to_the_query_string(self):
        """Verified live: a signature minted for page=1 is rejected on page=2."""
        a = sign_request("k", "GET", "/portal/export", "page=1", timestamp=1)
        b = sign_request("k", "GET", "/portal/export", "page=2", timestamp=1)
        assert a.signature != b.signature

    def test_signature_changes_every_second(self):
        a = sign_request("k", "GET", "/p", "", timestamp=1700000000)
        b = sign_request("k", "GET", "/p", "", timestamp=1700000001)
        assert a.signature != b.signature

    def test_headers_use_the_names_the_portal_expects(self):
        headers = sign_request("k", "GET", "/p", "", timestamp=1).as_headers()
        assert set(headers) == {"x-timestamp", "x-signature"}

    def test_documented_skew_window(self):
        assert MAX_CLOCK_SKEW_SECONDS == 300


class TestDevalue:
    def test_resolves_integer_references(self):
        # index 0 is the root; every integer is a pointer into the same array.
        assert unflatten([{"meterId": 1}, "J100001"]) == {"meterId": "J100001"}

    def test_shares_a_deduplicated_value_across_fields(self):
        flat = [{"a": 1, "b": 1}, "same"]
        assert unflatten(flat) == {"a": "same", "b": "same"}

    def test_resolves_nested_arrays_of_objects(self):
        flat = [[1, 3], {"n": 2}, "first", {"n": 4}, "second"]
        assert unflatten(flat) == [{"n": "first"}, {"n": "second"}]

    def test_negative_sentinels_become_none(self):
        assert unflatten([{"missing": -1}]) == {"missing": None}

    @pytest.mark.parametrize("payload", [[], "nope", {}])
    def test_rejects_malformed_payloads(self, payload):
        with pytest.raises(PortalProtocolError):
            unflatten(payload)

    def test_rejects_out_of_bounds_reference(self):
        with pytest.raises(PortalProtocolError, match="out of bounds"):
            unflatten([{"a": 99}])

    def test_rejects_cyclic_reference(self):
        with pytest.raises(PortalProtocolError, match="cyclic"):
            unflatten([{"self": 1}, {"back": 0}])

    def test_soft_redirect_is_surfaced_not_swallowed(self):
        """An expired session returns HTTP 200 with a redirect body - the nastiest trap."""
        with pytest.raises(PortalProtocolError, match="redirect"):
            extract_data_nodes({"type": "redirect", "location": "/login"})

    def test_embedded_error_node_is_surfaced(self):
        """An unknown meter also returns HTTP 200, with an error node inside."""
        payload = {
            "type": "data",
            "nodes": [
                None,
                {"type": "error", "error": {"message": "Meter not found"}, "status": 404},
            ],
        }
        with pytest.raises(PortalProtocolError, match="Meter not found"):
            extract_data_nodes(payload)

    def test_node_is_selected_by_shape_not_position(self, ssr_legacy):
        """Adding a layout upstream shifts node order; selecting by keys survives that."""
        node = find_node_with_keys(ssr_legacy, "detail", "hierarchy")
        assert node["meterId"] == "J100000"

    def test_missing_shape_raises(self, ssr_legacy):
        with pytest.raises(PortalProtocolError):
            find_node_with_keys(ssr_legacy, "definitely_absent")
