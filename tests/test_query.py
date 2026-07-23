"""Tests for the local query layer - the capabilities the portal does not have."""

from __future__ import annotations

import pytest

from app.domain.models import GeoPoint, InstallStatus, Meter, PhaseType
from app.store.snapshot import MeterQuery, haversine_km, meters_near, query_meters


def make_meter(meter_id: str, **kwargs) -> Meter:
    defaults = {
        "serial_no": None,
        "make": None,
        "phase_type": None,
        "install_status": None,
        "install_type": None,
        "build": None,
        "dt_code": None,
        "hierarchy": [],
        "location": None,
    }
    return Meter(meter_id=meter_id, **{**defaults, **kwargs})


@pytest.fixture
def estate() -> list[Meter]:
    return [
        make_meter(
            "J100001",
            serial_no="SE33962",
            make="HPL",
            install_status=InstallStatus.INSTALLED,
            phase_type=PhaseType.SINGLE,
            dt_code="DT-001",
            location=GeoPoint(latitude=26.9124, longitude=75.7873),
        ),
        make_meter(
            "J100002",
            serial_no="GE84132",
            make="L&T",
            install_status=InstallStatus.FAULTY,
            phase_type=PhaseType.THREE,
            dt_code="DT-002",
        ),
        make_meter(
            "J100003",
            serial_no="AL28136",
            make="HPL",
            install_status=InstallStatus.INSTALLED,
            phase_type=PhaseType.THREE,
            dt_code="DT-001",
            data_quality=["hierarchy_no_code_circle"],
        ),
    ]


class TestSearch:
    def test_matches_meter_id_substring(self, estate):
        assert len(query_meters(estate, MeterQuery(search="J10000"))) == 3

    def test_matches_serial_substring(self, estate):
        result = query_meters(estate, MeterQuery(search="GE84"))
        assert [m.meter_id for m in result] == ["J100002"]

    def test_is_case_insensitive(self, estate):
        assert query_meters(estate, MeterQuery(search="se33962"))

    @pytest.mark.parametrize("wildcard", ["%", "_", "%%", "SE%"])
    def test_sql_wildcards_are_literal_here(self, estate, wildcard):
        """Upstream, `q=_` matches all 403 meters. Locally it must match nothing."""
        assert query_meters(estate, MeterQuery(search=wildcard)) == []

    def test_no_search_returns_everything(self, estate):
        assert len(query_meters(estate, MeterQuery())) == 3


class TestFilters:
    def test_filter_by_make(self, estate):
        assert len(query_meters(estate, MeterQuery(make="HPL"))) == 2

    def test_filter_is_case_insensitive(self, estate):
        assert len(query_meters(estate, MeterQuery(make="hpl"))) == 2

    def test_filter_by_status_uses_canonical_value(self, estate):
        result = query_meters(estate, MeterQuery(install_status="faulty"))
        assert [m.meter_id for m in result] == ["J100002"]

    def test_filters_are_anded(self, estate):
        result = query_meters(estate, MeterQuery(make="HPL", phase_type="three"))
        assert [m.meter_id for m in result] == ["J100003"]

    def test_filter_by_dt_code(self, estate):
        assert len(query_meters(estate, MeterQuery(dt_code="DT-001"))) == 2

    def test_filter_by_presence_of_location(self, estate):
        assert len(query_meters(estate, MeterQuery(has_location=True))) == 1
        assert len(query_meters(estate, MeterQuery(has_location=False))) == 2

    def test_filter_by_data_quality_flags(self, estate):
        result = query_meters(estate, MeterQuery(with_issues=True))
        assert [m.meter_id for m in result] == ["J100003"]

    def test_unmatched_filter_returns_empty(self, estate):
        assert query_meters(estate, MeterQuery(make="Nonexistent")) == []


class TestSorting:
    def test_default_sort_is_by_id(self, estate):
        result = query_meters(estate, MeterQuery())
        assert [m.meter_id for m in result] == ["J100001", "J100002", "J100003"]

    def test_descending(self, estate):
        result = query_meters(estate, MeterQuery(descending=True))
        assert result[0].meter_id == "J100003"

    def test_sort_by_serial(self, estate):
        result = query_meters(estate, MeterQuery(sort="serial_no"))
        assert [m.serial_no for m in result] == ["AL28136", "GE84132", "SE33962"]

    def test_nulls_sort_last(self):
        meters = [make_meter("A", make=None), make_meter("B", make="HPL")]
        result = query_meters(meters, MeterQuery(sort="make"))
        assert result[0].meter_id == "B"

    def test_unknown_sort_field_falls_back_to_id(self, estate):
        result = query_meters(estate, MeterQuery(sort="not_a_field"))
        assert [m.meter_id for m in result] == ["J100001", "J100002", "J100003"]


class TestProximity:
    def test_known_distance(self):
        """Jaipur to Delhi is ~239 km; anything wildly off means a broken formula."""
        assert haversine_km(26.9124, 75.7873, 28.6139, 77.2090) == pytest.approx(239, abs=5)

    def test_identical_points_are_zero_apart(self):
        assert haversine_km(26.9, 75.8, 26.9, 75.8) == 0.0

    def test_finds_meters_inside_the_radius(self, estate):
        found = meters_near(estate, 26.9124, 75.7873, radius_km=1.0)
        assert [m.meter_id for m, _ in found] == ["J100001"]

    def test_excludes_meters_outside_the_radius(self, estate):
        assert meters_near(estate, 0.0, 0.0, radius_km=1.0) == []

    def test_meters_without_location_are_skipped(self, estate):
        found = meters_near(estate, 26.9124, 75.7873, radius_km=20000)
        assert len(found) == 1

    def test_results_are_nearest_first(self):
        meters = [
            make_meter("far", location=GeoPoint(latitude=27.0, longitude=75.9)),
            make_meter("near", location=GeoPoint(latitude=26.9125, longitude=75.7874)),
        ]
        found = meters_near(meters, 26.9124, 75.7873, radius_km=100)
        assert [m.meter_id for m, _ in found] == ["near", "far"]
