"""Tests for normalisation - the layer that absorbs the portal's inconsistencies.

The core assertion throughout: **both upstream builds produce the same canonical shape.**
That is the whole promise of this service.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.domain import normalize
from app.domain.models import HierarchyLevel, InstallStatus, InstallType, MeterBuild, PhaseType
from app.portal.devalue import find_node_with_keys


class TestScalarCoercion:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("124.50", 124.5),
            ("42594.05", 42594.05),
            (231, 231.0),
            ("1,024.5", 1024.5),
            ("  17 ", 17.0),
            ("", None),
            (None, None),
            ("N/A", None),
            ("abc", None),
        ],
    )
    def test_to_float(self, raw, expected):
        assert normalize.to_float(raw) == expected

    def test_booleans_are_not_numbers(self):
        """`float(True) == 1.0` in Python - a silent corruption if left unguarded."""
        assert normalize.to_float(True) is None

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("  spaced   out ", "spaced out"),
            ("", None),
            ("N/A", None),
            ("-", None),
            (None, None),
            ("Circle 1", "Circle 1"),
        ],
    )
    def test_clean_text(self, raw, expected):
        assert normalize.clean_text(raw) == expected

    def test_parses_day_first_timestamps(self):
        """Day-first is proven by the data: series run to 30/06, and there is no month 30."""
        assert normalize.parse_timestamp("24/06/2026 00:30") == datetime(2026, 6, 24, 0, 30)

    def test_parses_a_day_that_cannot_be_a_month(self):
        assert normalize.parse_timestamp("30/06/2026 23:30") == datetime(2026, 6, 30, 23, 30)

    def test_accepts_iso_as_a_fallback(self):
        assert normalize.parse_timestamp("2026-06-24T00:30:00") == datetime(2026, 6, 24, 0, 30)

    def test_unparseable_timestamp_returns_none(self):
        assert normalize.parse_timestamp("not a date") is None

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Feeder 2 (F-002)", ("Feeder 2", "F-002")),
            ("Malviya Nagar DT 1 (DT-001)", ("Malviya Nagar DT 1", "DT-001")),
            ("(F-003)", (None, "F-003")),
            ("Feeder 5", ("Feeder 5", None)),
            ("", (None, None)),
        ],
    )
    def test_split_name_and_code(self, raw, expected):
        assert normalize.split_name_and_code(raw) == expected


class TestMeterFromExport:
    def test_maps_every_field_to_canonical_types(self, export_rows):
        row = next(r for r in export_rows if r["meterId"] == "J100000")
        meter = normalize.meter_from_export(row)
        assert meter.meter_id == "J100000"
        assert meter.serial_no == "SE33962"
        assert meter.install_status is InstallStatus.DECOMMISSIONED
        assert meter.phase_type is PhaseType.SINGLE
        assert meter.install_type is InstallType.WHOLE_CURRENT
        assert meter.build is MeterBuild.LEGACY

    def test_coordinates_become_floats(self, export_rows):
        meter = normalize.meter_from_export(export_rows[0])
        assert isinstance(meter.location.latitude, float)

    def test_hierarchy_is_ordered_root_to_leaf(self, export_rows):
        meter = normalize.meter_from_export(export_rows[0])
        assert [ref.level for ref in meter.hierarchy] == [
            HierarchyLevel.ZONE,
            HierarchyLevel.CIRCLE,
            HierarchyLevel.DIVISION,
            HierarchyLevel.SUBDIVISION,
            HierarchyLevel.SUBSTATION,
            HierarchyLevel.FEEDER,
            HierarchyLevel.DT,
        ]

    def test_blank_code_is_none_not_empty_string(self, export_rows):
        """A caller must be able to tell 'unknown' from 'deliberately blank'."""
        row = next(r for r in export_rows if r["meterId"] == "J100011")
        meter = normalize.meter_from_export(row)
        circle = next(r for r in meter.hierarchy if r.level is HierarchyLevel.CIRCLE)
        assert circle.code is None
        assert circle.name == "Circle 6"

    def test_blank_field_is_flagged_for_reporting(self, export_rows):
        row = next(r for r in export_rows if r["meterId"] == "J100011")
        meter = normalize.meter_from_export(row)
        assert "hierarchy_no_code_circle" in meter.data_quality

    def test_row_without_meter_id_is_rejected(self):
        with pytest.raises(ValueError):
            normalize.meter_from_export({"serialNo": "X"})

    def test_unknown_enum_degrades_to_none_rather_than_raising(self):
        meter = normalize.meter_from_export({"meterId": "J1", "installStatus": "Levitating"})
        assert meter.install_status is None
        assert meter.meter_id == "J1"


class TestBothDetailShapesConverge:
    """The central claim of the normaliser, asserted directly."""

    def test_legacy_parameter_rows_are_parsed(self, ssr_legacy):
        node = find_node_with_keys(ssr_legacy, "detail", "hierarchy")
        meter = normalize.meter_from_ssr_detail("J100000", node)
        assert meter.build is MeterBuild.LEGACY
        assert meter.serial_no == "SE33962"
        assert meter.make == "HPL"
        assert meter.install_status is InstallStatus.DECOMMISSIONED

    def test_v2_double_encoded_class_data_is_parsed(self, ssr_v2):
        node = find_node_with_keys(ssr_v2, "detail", "hierarchy")
        meter = normalize.meter_from_ssr_detail("J100004", node)
        assert meter.build is MeterBuild.V2
        assert meter.serial_no == "SE65293"
        assert meter.make == "Genus"
        assert meter.install_status is InstallStatus.FAULTY

    def test_both_shapes_produce_identical_field_sets(self, ssr_legacy, ssr_v2):
        legacy = normalize.meter_from_ssr_detail(
            "J100000", find_node_with_keys(ssr_legacy, "detail", "hierarchy")
        )
        v2 = normalize.meter_from_ssr_detail(
            "J100004", find_node_with_keys(ssr_v2, "detail", "hierarchy")
        )

        def populated(meter):
            return {k for k, v in meter.model_dump().items() if v not in (None, [], "")}

        assert populated(legacy) == populated(v2)

    def test_ssr_hierarchy_display_strings_are_split(self, ssr_v2):
        node = find_node_with_keys(ssr_v2, "detail", "hierarchy")
        meter = normalize.meter_from_ssr_detail("J100004", node)
        dt = next(r for r in meter.hierarchy if r.level is HierarchyLevel.DT)
        assert (dt.name, dt.code) == ("Bani Park DT 5", "DT-005")

    def test_sub_station_label_with_a_space_is_recognised(self, ssr_v2):
        """The SSR page uses 'Sub Station'; the export uses 'substation'."""
        node = find_node_with_keys(ssr_v2, "detail", "hierarchy")
        meter = normalize.meter_from_ssr_detail("J100004", node)
        assert any(r.level is HierarchyLevel.SUBSTATION for r in meter.hierarchy)

    def test_unparseable_class_data_is_flagged_not_crashed(self):
        meter = normalize.meter_from_ssr_detail(
            "J1", {"detail": {"classData": "{not json"}, "hierarchy": {}}
        )
        assert "class_data_unparseable" in meter.data_quality
        assert meter.meter_id == "J1"

    def test_unrecognised_detail_shape_is_flagged(self):
        meter = normalize.meter_from_ssr_detail(
            "J1", {"detail": {"somethingNew": 1}, "hierarchy": {}}
        )
        assert "detail_shape_unrecognised" in meter.data_quality


class TestReadingsAndTransformers:
    def test_reading_values_become_floats(self, energy_30min):
        reading = normalize.reading_from_row(energy_30min[0])
        assert isinstance(reading.kwh, float)
        assert isinstance(reading.timestamp, datetime)

    def test_reading_without_timestamp_is_dropped(self):
        assert normalize.reading_from_row({"kwh": "1.0"}) is None

    def test_transformer_row_is_typed(self, dt_rows):
        transformer = normalize.transformer_from_row(dt_rows[0])
        assert transformer.code == "DT-001"
        assert isinstance(transformer.capacity_kva, int)

    def test_transformer_without_code_is_dropped(self):
        assert normalize.transformer_from_row({"name": "orphan"}) is None

    def test_geo_endpoint_strings_become_floats(self):
        point = normalize.geo_from_endpoint(
            {"latitude": "26.822136543835608", "longitude": "75.90718190602279"}
        )
        assert point.latitude == pytest.approx(26.822136543835608)

    def test_out_of_range_coordinates_are_rejected(self):
        assert normalize.geo_from_endpoint({"latitude": "999", "longitude": "0"}) is None
