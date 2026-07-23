"""Tests for consumption derivation.

The headline regression these protect against: treating the portal's **cumulative
register** values as per-interval usage. Doing so overstates a household's weekly
consumption by roughly five orders of magnitude, and it looks entirely plausible in a
JSON response - which is exactly why it needs a test rather than a comment.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.domain import consumption as ops
from app.domain.models import MeterReading
from app.domain.normalize import reading_from_row


def reading(minutes: int, kwh: float, kvah: float | None = None, volts: float = 230.0):
    return MeterReading(
        timestamp=datetime(2026, 6, 24) + timedelta(minutes=minutes),
        kwh=kwh,
        kvah=kvah,
        voltage_r=volts,
    )


class TestRegistersAreCumulative:
    def test_consumption_is_the_delta_not_the_reading(self):
        readings = [reading(0, 1000.0), reading(30, 1000.5), reading(60, 1001.0)]
        result = ops.build_response("M1", readings)
        assert result.summary.total_kwh == 1.0  # not 3001.5

    def test_totals_match_last_minus_first(self, energy_30min):
        readings = [reading_from_row(r) for r in energy_30min]
        result = ops.build_response("J100001", readings)
        expected = round(readings[-1].kwh - readings[0].kwh, 3)
        assert result.summary.total_kwh == pytest.approx(expected, abs=1e-6)

    def test_a_register_decrease_is_not_negative_consumption(self):
        """A rollover or meter swap; the true value is unknowable from two samples."""
        readings = [reading(0, 1000.0), reading(30, 5.0), reading(60, 6.0)]
        intervals = ops.to_intervals(readings, 30)
        assert intervals[0].kwh is None
        assert intervals[1].kwh == 1.0

    def test_totals_ignore_the_unknown_interval(self):
        readings = [reading(0, 1000.0), reading(30, 5.0), reading(60, 6.0)]
        result = ops.build_response("M1", readings)
        assert result.summary.total_kwh == 1.0


class TestCadenceIsInferred:
    def test_thirty_minute_cadence(self, energy_30min):
        readings = [reading_from_row(r) for r in energy_30min]
        assert ops.infer_interval_minutes(readings) == 30.0

    def test_daily_cadence_on_the_same_code_path(self, energy_daily):
        """J100400-J100402 sample daily. A hardcoded 30 would mislabel every interval."""
        readings = [reading_from_row(r) for r in energy_daily]
        assert ops.infer_interval_minutes(readings) == 1440.0

    def test_mode_not_mean_survives_a_gap(self):
        readings = [reading(0, 1.0), reading(30, 2.0), reading(60, 3.0), reading(600, 4.0)]
        assert ops.infer_interval_minutes(readings) == 30.0

    def test_single_reading_has_no_inferable_cadence(self):
        assert ops.infer_interval_minutes([reading(0, 1.0)]) is None

    def test_gap_beyond_tolerance_is_marked_estimated(self):
        readings = [reading(0, 1.0), reading(30, 2.0), reading(60, 3.0), reading(600, 4.0)]
        intervals = ops.to_intervals(readings, 30.0)
        assert [i.estimated for i in intervals] == [False, False, True]


class TestOrderingAndWindowing:
    def test_unordered_input_is_sorted(self):
        readings = [reading(60, 3.0), reading(0, 1.0), reading(30, 2.0)]
        prepared = ops.prepare_readings(readings)
        assert [r.kwh for r in prepared] == [1.0, 2.0, 3.0]

    def test_duplicate_timestamps_are_dropped(self):
        readings = [reading(0, 1.0), reading(0, 1.0), reading(30, 2.0)]
        assert len(ops.prepare_readings(readings)) == 2

    def test_window_filters_locally(self):
        """The portal ignores date parameters, so this must happen on our side."""
        readings = [reading(0, 1.0), reading(30, 2.0), reading(60, 3.0)]
        windowed = ops.filter_window(
            readings, datetime(2026, 6, 24, 0, 30), datetime(2026, 6, 24, 1, 0)
        )
        assert len(windowed) == 2

    def test_window_with_no_readings_yields_empty_summary(self):
        result = ops.build_response("M1", [reading(0, 1.0)], start=datetime(2030, 1, 1))
        assert result.summary.reading_count == 0
        assert result.summary.total_kwh is None
        assert result.window_start is None


class TestResampling:
    def test_daily_rollup_sums_the_intervals(self):
        readings = [reading(i * 30, 100.0 + i * 0.5) for i in range(48)]
        result = ops.build_response("M1", readings, granularity="daily")
        assert len(result.intervals) == 1
        assert result.intervals[0].kwh == pytest.approx(23.5)

    def test_hourly_rollup_buckets_by_hour(self):
        readings = [reading(i * 30, 100.0 + i * 0.5) for i in range(5)]
        result = ops.build_response("M1", readings, granularity="hourly")
        assert len(result.intervals) == 2

    def test_rollup_preserves_the_total(self):
        readings = [reading(i * 30, 100.0 + i * 0.5) for i in range(48)]
        raw = ops.build_response("M1", readings)
        daily = ops.build_response("M1", readings, granularity="daily")
        assert sum(i.kwh for i in daily.intervals) == pytest.approx(raw.summary.total_kwh)

    def test_voltage_average_is_duration_weighted(self):
        readings = [
            reading(0, 1.0, volts=200.0),
            reading(30, 2.0, volts=240.0),
            reading(180, 3.0, volts=240.0),
        ]
        result = ops.build_response("M1", readings, granularity="daily")
        # 30-min interval @220 mean, 150-min interval @240 mean -> weighted toward 240.
        assert result.intervals[0].average_voltage_r > 232

    def test_peak_matches_the_returned_granularity(self):
        """A 30-minute peak reported next to daily buckets reads as a bug."""
        readings = [reading(i * 30, 100.0 + i * 0.5) for i in range(48)]
        raw = ops.build_response("M1", readings)
        daily = ops.build_response("M1", readings, granularity="daily")
        assert raw.summary.peak_interval_kwh == pytest.approx(0.5)
        assert daily.summary.peak_interval_kwh == pytest.approx(23.5)

    def test_totals_are_unaffected_by_granularity(self):
        readings = [reading(i * 30, 100.0 + i * 0.5) for i in range(48)]
        raw = ops.build_response("M1", readings)
        daily = ops.build_response("M1", readings, granularity="daily")
        assert raw.summary.total_kwh == daily.summary.total_kwh

    def test_estimated_flag_propagates_through_rollup(self):
        readings = [reading(0, 1.0), reading(30, 2.0), reading(600, 3.0)]
        result = ops.build_response("M1", readings, granularity="daily")
        assert result.intervals[0].estimated is True


class TestSummary:
    def test_power_factor_is_kwh_over_kvah(self):
        readings = [reading(0, 100.0, kvah=100.0), reading(30, 101.0, kvah=102.0)]
        result = ops.build_response("M1", readings)
        assert result.summary.average_power_factor == pytest.approx(0.5)

    def test_power_factor_is_none_without_kvah(self):
        readings = [reading(0, 100.0), reading(30, 101.0)]
        assert ops.build_response("M1", readings).summary.average_power_factor is None

    def test_zero_kvah_does_not_divide_by_zero(self):
        readings = [reading(0, 100.0, kvah=5.0), reading(30, 101.0, kvah=5.0)]
        assert ops.build_response("M1", readings).summary.average_power_factor is None

    def test_voltage_bounds_come_from_readings(self, energy_30min):
        readings = [reading_from_row(r) for r in energy_30min]
        summary = ops.build_response("J100001", readings).summary
        assert summary.min_voltage_r <= summary.max_voltage_r

    def test_readings_are_omitted_unless_requested(self, energy_30min):
        readings = [reading_from_row(r) for r in energy_30min]
        assert ops.build_response("M1", readings).readings == []
        assert ops.build_response("M1", readings, include_readings=True).readings

    def test_empty_series_does_not_crash(self):
        result = ops.build_response("M1", [])
        assert result.summary.reading_count == 0
        assert result.intervals == []
