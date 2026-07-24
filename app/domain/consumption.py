"""Derivation of consumption from the portal's cumulative meter registers.

The single most important fact about this data, and the easiest thing to get wrong:

    ``kwh`` and ``kvah`` are **cumulative register readings**, not per-interval usage.

Verified across every sampled meter - the series is monotonically non-decreasing, and
``J100001`` runs 42594.05 -> 42732.85 over seven days. Summing that column would report
~14 million kWh for a household. Consumption is the **difference between consecutive
readings**, which is what this module computes.

Two further verified properties shape the implementation:

* **Cadence is not fixed.** Most meters sample every 30 minutes (337 points over 7 days),
  but ``J100400``-``J100402`` sample **daily** (8 points). The interval is therefore
  inferred from the data - taking the mode of observed gaps - and never assumed.
* **Registers can roll over.** Not observed in this dataset, but physical meters wrap at
  their digit limit. A negative delta is treated as a rollover or a meter replacement and
  excluded rather than reported as negative consumption.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from itertools import pairwise

from .models import (
    ConsumptionInterval,
    ConsumptionResponse,
    ConsumptionSummary,
    MeterReading,
)

logger = logging.getLogger(__name__)

#: A gap larger than this multiple of the inferred cadence is treated as missing data
#: rather than a long interval, and its consumption is marked ``estimated``.
GAP_TOLERANCE_FACTOR = 1.5

Granularity = str  # "raw" | "hourly" | "daily"
VALID_GRANULARITIES: frozenset[str] = frozenset({"raw", "hourly", "daily"})


def prepare_readings(readings: Iterable[MeterReading]) -> list[MeterReading]:
    """Sort by time and drop exact duplicate timestamps.

    Ordering is not guaranteed by the portal's contract, only observed - so we enforce it
    rather than depend on it. Duplicates would otherwise produce a zero-length interval and
    a division by zero when computing rates.
    """
    ordered = sorted(readings, key=lambda r: r.timestamp)
    deduped: list[MeterReading] = []
    seen: set[datetime] = set()
    for reading in ordered:
        if reading.timestamp in seen:
            continue
        seen.add(reading.timestamp)
        deduped.append(reading)
    return deduped


def infer_interval_minutes(readings: Sequence[MeterReading]) -> float | None:
    """Infer sampling cadence as the modal gap between consecutive readings.

    The mode, not the mean: a single missing block would drag a mean upward and
    mis-classify every healthy interval that follows as a gap.
    """
    if len(readings) < 2:
        return None
    gaps = Counter(
        round((b.timestamp - a.timestamp).total_seconds() / 60.0, 3)
        for a, b in pairwise(readings)
    )
    gaps.pop(0.0, None)
    if not gaps:
        return None
    return gaps.most_common(1)[0][0]


def filter_window(
    readings: Sequence[MeterReading],
    start: datetime | None,
    end: datetime | None,
) -> list[MeterReading]:
    """Restrict readings to ``[start, end]``.

    Range filtering happens here because the portal ignores every date parameter it is
    given (verified) and always returns its full seven-day window.
    """
    result = list(readings)
    if start is not None:
        result = [r for r in result if r.timestamp >= start]
    if end is not None:
        result = [r for r in result if r.timestamp <= end]
    return result


def to_intervals(
    readings: Sequence[MeterReading], interval_minutes: float | None
) -> list[ConsumptionInterval]:
    """Difference consecutive register readings into per-interval consumption."""
    intervals: list[ConsumptionInterval] = []
    tolerance = (
        timedelta(minutes=interval_minutes * GAP_TOLERANCE_FACTOR)
        if interval_minutes
        else None
    )

    for previous, current in pairwise(readings):
        span = current.timestamp - previous.timestamp
        duration_minutes = span.total_seconds() / 60.0
        if duration_minutes <= 0:
            continue
        intervals.append(
            ConsumptionInterval(
                start=previous.timestamp,
                end=current.timestamp,
                duration_minutes=round(duration_minutes, 3),
                kwh=_delta(previous.kwh, current.kwh),
                kvah=_delta(previous.kvah, current.kvah),
                average_voltage_r=_mean(previous.voltage_r, current.voltage_r),
                estimated=bool(tolerance and span > tolerance),
            )
        )
    return intervals


def resample(
    intervals: Sequence[ConsumptionInterval], granularity: Granularity
) -> list[ConsumptionInterval]:
    """Aggregate intervals into hourly or daily buckets.

    Buckets are keyed by the interval's **start**, so an interval is attributed to the
    period in which the energy was consumed. Voltage is averaged weighted by duration -
    an unweighted mean would let a short interval count as much as a long one.
    """
    if granularity == "raw" or not intervals:
        return list(intervals)

    buckets: dict[datetime, list[ConsumptionInterval]] = {}
    for interval in intervals:
        key = _bucket_key(interval.start, granularity)
        buckets.setdefault(key, []).append(interval)

    step = timedelta(hours=1) if granularity == "hourly" else timedelta(days=1)
    aggregated: list[ConsumptionInterval] = []
    for key in sorted(buckets):
        members = buckets[key]
        weighted = [
            (m.average_voltage_r, m.duration_minutes)
            for m in members
            if m.average_voltage_r is not None and m.duration_minutes > 0
        ]
        total_weight = sum(weight for _, weight in weighted)
        aggregated.append(
            ConsumptionInterval(
                start=key,
                end=key + step,
                duration_minutes=round(sum(m.duration_minutes for m in members), 3),
                kwh=_sum_optional(m.kwh for m in members),
                kvah=_sum_optional(m.kvah for m in members),
                average_voltage_r=(
                    round(sum(v * w for v, w in weighted) / total_weight, 3)
                    if total_weight
                    else None
                ),
                estimated=any(m.estimated for m in members),
            )
        )
    return aggregated


def summarise(
    readings: Sequence[MeterReading],
    intervals: Sequence[ConsumptionInterval],
    interval_minutes: float | None,
    reported_intervals: Sequence[ConsumptionInterval] | None = None,
) -> ConsumptionSummary:
    """Aggregate statistics over the requested window.

    ``intervals`` are the raw per-reading deltas; ``reported_intervals`` are what the
    caller actually receives after resampling. Totals and gap counts come from the raw
    series, but the peak is taken from the reported series - a "peak" that referred to a
    granularity the caller never asked for would be actively misleading (a 30-minute peak
    of 0.42 kWh sitting beside daily buckets of ~20 kWh reads like a bug).
    """
    reported = intervals if reported_intervals is None else reported_intervals
    voltages = [r.voltage_r for r in readings if r.voltage_r is not None]
    kwh_values = [i.kwh for i in intervals if i.kwh is not None]
    kvah_values = [i.kvah for i in intervals if i.kvah is not None]
    peak_values = [i.kwh for i in reported if i.kwh is not None]

    total_kwh = round(sum(kwh_values), 3) if kwh_values else None
    total_kvah = round(sum(kvah_values), 3) if kvah_values else None

    return ConsumptionSummary(
        total_kwh=total_kwh,
        total_kvah=total_kvah,
        average_power_factor=(
            round(total_kwh / total_kvah, 4)
            if total_kwh is not None and total_kvah not in (None, 0)
            else None
        ),
        peak_interval_kwh=round(max(peak_values), 3) if peak_values else None,
        min_voltage_r=min(voltages) if voltages else None,
        max_voltage_r=max(voltages) if voltages else None,
        interval_minutes=interval_minutes,
        reading_count=len(readings),
        gap_count=sum(1 for i in intervals if i.estimated),
    )


def build_response(
    meter_id: str,
    raw_readings: Sequence[MeterReading],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    granularity: Granularity = "raw",
    include_readings: bool = False,
) -> ConsumptionResponse:
    """Full consumption pipeline: order -> window -> difference -> resample -> summarise.

    The cadence is inferred from the **windowed** readings so a narrow window over a
    daily-sampled meter is not misjudged against the 30-minute majority.
    """
    ordered = prepare_readings(raw_readings)
    windowed = filter_window(ordered, start, end)
    interval_minutes = infer_interval_minutes(windowed)
    intervals = to_intervals(windowed, interval_minutes)
    aggregated = resample(intervals, granularity)

    return ConsumptionResponse(
        meter_id=meter_id,
        window_start=windowed[0].timestamp if windowed else None,
        window_end=windowed[-1].timestamp if windowed else None,
        granularity=granularity,
        summary=summarise(windowed, intervals, interval_minutes, aggregated),
        intervals=aggregated,
        readings=list(windowed) if include_readings else [],
    )


def _delta(previous: float | None, current: float | None) -> float | None:
    """Consumption between two register readings.

    A negative result means the register wrapped or the meter was replaced; the true
    consumption is unknowable from two samples, so we return ``None`` rather than a
    negative number that would corrupt any downstream sum.
    """
    if previous is None or current is None:
        return None
    difference = current - previous
    if difference < 0:
        logger.warning(
            "register decreased between readings",
            extra={"previous": previous, "current": current},
        )
        return None
    return round(difference, 3)


def _mean(*values: float | None) -> float | None:
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), 3) if present else None


def _sum_optional(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(sum(present), 3) if present else None


def _bucket_key(moment: datetime, granularity: Granularity) -> datetime:
    if granularity == "hourly":
        return moment.replace(minute=0, second=0, microsecond=0)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)
