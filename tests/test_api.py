"""End-to-end API contract tests.

These run against the real application - real routing, real serialisation, real error
handlers - with only the *network* replaced. The snapshot is built by the genuine
normalisation and hierarchy pipeline from recorded portal payloads, so these tests assert
the contract a client actually receives.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_session, get_snapshot, get_store
from app.config import Settings
from app.domain import hierarchy as hierarchy_ops
from app.domain import normalize
from app.domain.quality import build_report
from app.main import create_app
from app.portal.exceptions import PortalNotFound, PortalUnavailable
from app.store.snapshot import Snapshot

from .conftest import load_fixture


def build_snapshot() -> Snapshot:
    """Run the real pipeline over recorded payloads."""
    rows = load_fixture("export_sample.json")["data"]
    dt_rows = load_fixture("dts_page1.json")["data"]
    meters = [normalize.meter_from_export(r) for r in rows]
    transformers = [t for t in (normalize.transformer_from_row(r) for r in dt_rows) if t]
    repaired, report = hierarchy_ops.repair_blanks(meters, transformers)
    counts: dict[str, int] = {}
    for meter in repaired:
        if meter.dt_code:
            counts[meter.dt_code] = counts.get(meter.dt_code, 0) + 1
    transformers = [
        t.model_copy(update={"meter_count": counts.get(t.code, 0)}) for t in transformers
    ]
    return Snapshot(
        built_at=datetime.now(UTC),
        source="export",
        meters=tuple(repaired),
        meters_by_id={m.meter_id: m for m in repaired},
        transformers=tuple(transformers),
        transformers_by_code={t.code: t for t in transformers},
        tree=hierarchy_ops.build_tree(repaired),
        quality=build_report(repaired, report),
        build_duration_ms=1.0,
    )


class FakeStore:
    """Stands in for SnapshotStore without touching the network."""

    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot
        self.readings_error: Exception | None = None
        self.refresh_calls = 0

    async def get(self, *, force_refresh: bool = False) -> Snapshot:
        if force_refresh:
            self.refresh_calls += 1
        return self.snapshot

    async def get_readings(self, meter_id: str):
        if self.readings_error:
            raise self.readings_error
        rows = load_fixture("energy_30min.json")["data"]
        return [r for r in (normalize.reading_from_row(row) for row in rows) if r]

    def info(self):
        from app.domain.models import SnapshotInfo

        return SnapshotInfo(
            built_at=self.snapshot.built_at,
            age_seconds=1.0,
            ttl_seconds=300,
            is_stale=False,
            meter_count=len(self.snapshot.meters),
            transformer_count=len(self.snapshot.transformers),
            source="export",
            build_duration_ms=1.0,
            refresh_count=1,
            last_error=None,
        )


class FakeSession:
    """Stands in for PortalSession in the readiness and session-status routes."""

    def __init__(self) -> None:
        self.remote_ok = True
        self.error: Exception | None = None
        self.expires_at = datetime.now(UTC)
        self.authenticated_at = datetime.now(UTC)
        self.login_count = 1
        self.is_authenticated = True

    async def fetch_remote_session(self):
        if self.error:
            raise self.error
        return {"session": {"token": "t"}} if self.remote_ok else None


@pytest.fixture
def store() -> FakeStore:
    return FakeStore(build_snapshot())


@pytest.fixture
def session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def client(store: FakeStore, session: FakeSession):
    settings = Settings(
        portal_username="test",
        portal_password="test",
        snapshot_refresh_on_start=False,
        log_format="console",
    )
    app = create_app(settings)
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_snapshot] = lambda: store.snapshot
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as test_client:
        yield test_client


class TestMeterListing:
    def test_returns_a_paged_envelope(self, client):
        body = client.get("/api/v1/meters").json()
        assert set(body) == {"items", "meta"}
        assert body["meta"]["total_items"] == len(body["items"])

    def test_pagination_splits_results(self, client):
        body = client.get("/api/v1/meters?page_size=2").json()
        assert len(body["items"]) == 2
        assert body["meta"]["has_next"] is True
        assert body["meta"]["has_previous"] is False

    def test_second_page_differs_from_first(self, client):
        first = client.get("/api/v1/meters?page_size=2&page=1").json()["items"]
        second = client.get("/api/v1/meters?page_size=2&page=2").json()["items"]
        assert {m["meter_id"] for m in first}.isdisjoint({m["meter_id"] for m in second})

    def test_filter_by_make(self, client):
        body = client.get("/api/v1/meters?make=HPL").json()
        assert body["items"] and all(m["make"] == "HPL" for m in body["items"])

    def test_filter_by_status_uses_canonical_lowercase(self, client):
        body = client.get("/api/v1/meters?install_status=decommissioned").json()
        assert all(m["install_status"] == "decommissioned" for m in body["items"])

    def test_sql_wildcard_is_literal(self, client):
        """Upstream `q=%` returns everything; here it must return nothing."""
        assert client.get("/api/v1/meters?search=%25").json()["items"] == []

    def test_unknown_sort_field_is_rejected_with_options(self, client):
        response = client.get("/api/v1/meters?sort=bogus")
        assert response.status_code == 422
        assert "allowed" in response.json()["error"]["details"]

    def test_out_of_range_page_size_is_rejected(self, client):
        assert client.get("/api/v1/meters?page_size=9999").status_code == 422

    def test_snapshot_provenance_headers_are_present(self, client):
        headers = client.get("/api/v1/meters").headers
        assert "x-snapshot-age-seconds" in headers
        assert headers["x-snapshot-source"] == "export"


class TestMeterDetail:
    def test_returns_normalised_meter(self, client):
        body = client.get("/api/v1/meters/J100000").json()
        assert body["meter_id"] == "J100000"
        assert body["install_status"] == "decommissioned"
        assert len(body["hierarchy"]) == 7

    def test_hierarchy_is_root_to_leaf(self, client):
        body = client.get("/api/v1/meters/J100000").json()
        assert [r["level"] for r in body["hierarchy"]][:2] == ["zone", "circle"]

    def test_unknown_meter_returns_the_error_contract(self, client):
        response = client.get("/api/v1/meters/NOPE")
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "not_found"
        assert error["request_id"]

    def test_meter_ids_are_case_sensitive(self, client):
        assert client.get("/api/v1/meters/j100000").status_code == 404


class TestConsumption:
    def test_returns_derived_consumption_not_registers(self, client):
        body = client.get("/api/v1/meters/J100001/consumption").json()
        assert body["summary"]["total_kwh"] < 10  # a delta, not a ~42,000 register value
        assert body["summary"]["interval_minutes"] == 30.0

    def test_readings_omitted_by_default(self, client):
        assert client.get("/api/v1/meters/J100001/consumption").json()["readings"] == []

    def test_readings_included_on_request(self, client):
        body = client.get("/api/v1/meters/J100001/consumption?include_readings=true").json()
        assert body["readings"]

    def test_granularity_is_validated(self, client):
        response = client.get("/api/v1/meters/J100001/consumption?granularity=weekly")
        assert response.status_code == 422
        assert "allowed" in response.json()["error"]["details"]

    def test_inverted_window_is_rejected(self, client):
        response = client.get(
            "/api/v1/meters/J100001/consumption?from=2026-07-01&to=2026-06-01"
        )
        assert response.status_code == 422

    def test_unknown_meter_is_404_before_any_upstream_call(self, client):
        assert client.get("/api/v1/meters/NOPE/consumption").status_code == 404

    def test_portal_not_found_becomes_404(self, client, store):
        store.readings_error = PortalNotFound("gone")
        assert client.get("/api/v1/meters/J100001/consumption").status_code == 404

    def test_portal_outage_becomes_503_not_500(self, client, store):
        """A caller must be able to tell 'upstream is down' from 'this service is broken'."""
        store.readings_error = PortalUnavailable("portal down")
        response = client.get("/api/v1/meters/J100001/consumption")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "upstream_unavailable"

    def test_upstream_error_never_leaks_internals(self, client, store):
        store.readings_error = PortalUnavailable("connect to 10.0.0.5:5432 refused")
        body = client.get("/api/v1/meters/J100001/consumption").json()
        assert "10.0.0.5" not in body["error"]["message"]


class TestHierarchy:
    def test_returns_a_tree_rooted_at_network(self, client):
        body = client.get("/api/v1/hierarchy").json()
        assert body["id"] == "network"
        assert body["meter_count"] == 7

    def test_depth_limits_the_payload(self, client):
        body = client.get("/api/v1/hierarchy?depth=1").json()
        assert all(child["children"] == [] for child in body["children"])

    def test_node_ids_are_path_based(self, client):
        body = client.get("/api/v1/hierarchy?depth=2").json()
        assert body["children"][0]["children"][0]["id"].count("/") == 1

    def test_subtree_lookup_by_path(self, client):
        tree = client.get("/api/v1/hierarchy?depth=1").json()
        node_id = tree["children"][0]["id"]
        assert client.get(f"/api/v1/hierarchy/nodes/{node_id}").json()["id"] == node_id

    def test_unknown_node_returns_404_with_a_hint(self, client):
        response = client.get("/api/v1/hierarchy/nodes/Z-99")
        assert response.status_code == 404
        assert "hint" in response.json()["error"]["details"]

    def test_meters_under_a_node(self, client):
        tree = client.get("/api/v1/hierarchy?depth=1").json()
        node_id = tree["children"][0]["id"]
        body = client.get(f"/api/v1/hierarchy/nodes/{node_id}/meters").json()
        assert body["meta"]["total_items"] == tree["children"][0]["meter_count"]


class TestTransformers:
    def test_lists_registry_with_meter_counts(self, client):
        body = client.get("/api/v1/transformers").json()
        assert body["items"] and "meter_count" in body["items"][0]

    def test_filter_by_feeder(self, client):
        body = client.get("/api/v1/transformers?feeder_code=F-001").json()
        assert all(t["feeder_code"] == "F-001" for t in body["items"])

    def test_unknown_transformer_is_404(self, client):
        assert client.get("/api/v1/transformers/DT-999").status_code == 404

    def test_registry_name_wins_over_meter_alias(self, client):
        """DT-007 carries a stale alias on some meters; the registry value must win."""
        meter = client.get("/api/v1/meters/J100400").json()
        dt = next(r for r in meter["hierarchy"] if r["level"] == "dt")
        assert dt["name"] == "Sanganer DT 7"


class TestProximity:
    def test_finds_nearby_meters_sorted_by_distance(self, client):
        meter = client.get("/api/v1/meters/J100000").json()
        loc = meter["location"]
        body = client.get(
            f"/api/v1/meters/near?lat={loc['latitude']}&lng={loc['longitude']}&radius_km=50"
        ).json()
        assert body[0]["meter_id"] == "J100000"
        assert body == sorted(body, key=lambda m: m["distance_km"])

    def test_invalid_coordinates_are_rejected(self, client):
        assert client.get("/api/v1/meters/near?lat=999&lng=0").status_code == 422

    def test_near_route_is_not_shadowed_by_the_id_route(self, client):
        assert client.get("/api/v1/meters/near?lat=0&lng=0").status_code == 200


class TestSystem:
    def test_liveness_does_not_depend_on_upstream(self, client):
        assert client.get("/api/v1/health/live").json()["status"] == "alive"

    def test_snapshot_info_reports_provenance(self, client):
        body = client.get("/api/v1/system/snapshot").json()
        assert body["source"] == "export" and body["meter_count"] == 7

    def test_forced_refresh_calls_the_store(self, client, store):
        assert client.post("/api/v1/system/snapshot/refresh").status_code == 200
        assert store.refresh_calls == 1

    def test_data_quality_reports_upstream_anomalies(self, client):
        body = client.get("/api/v1/data-quality").json()
        assert body["issue_count"] > 0
        assert all(
            {"code", "level", "message", "affected_count"} <= set(i) for i in body["issues"]
        )

    def test_stats_summarises_the_estate(self, client):
        body = client.get("/api/v1/stats").json()
        assert body["meter_count"] == 7
        assert "by_install_status" in body

    def test_readiness_reports_both_dependency_checks(self, client):
        body = client.get("/api/v1/health/ready").json()
        assert body["status"] == "ready"
        assert set(body["checks"]) == {"portal_session", "snapshot"}

    def test_readiness_returns_503_when_the_session_is_rejected(self, client, session):
        """A load balancer must drain this instance rather than let it serve errors."""
        session.remote_ok = False
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"

    def test_readiness_survives_an_unreachable_portal(self, client, session):
        """The probe reports not-ready; it must not itself raise."""
        session.error = PortalUnavailable("portal down")
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["portal_session"]["ok"] is False

    def test_session_status_exposes_expiry_and_login_count(self, client):
        body = client.get("/api/v1/system/session").json()
        assert body["authenticated"] is True
        assert body["expires_at"] and body["login_count"] == 1

    def test_root_points_at_the_documentation(self, client):
        assert client.get("/").json()["documentation"] == "/docs"


class TestCrossCutting:
    def test_request_id_is_echoed(self, client):
        assert client.get("/api/v1/meters").headers["x-request-id"]

    def test_inbound_request_id_is_honoured(self, client):
        response = client.get("/api/v1/meters", headers={"X-Request-ID": "trace-123"})
        assert response.headers["x-request-id"] == "trace-123"

    def test_security_headers_are_applied(self, client):
        headers = client.get("/api/v1/meters").headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"

    def test_unknown_route_uses_the_error_contract(self, client):
        body = client.get("/api/v1/nope").json()
        assert body["error"]["code"] == "not_found"

    def test_openapi_document_is_served(self, client):
        spec = client.get("/openapi.json").json()
        assert spec["openapi"].startswith("3.")
        assert "/api/v1/meters" in spec["paths"]

    def test_docs_page_renders(self, client):
        assert client.get("/docs").status_code == 200
