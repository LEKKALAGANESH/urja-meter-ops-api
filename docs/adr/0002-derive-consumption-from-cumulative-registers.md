# ADR-0002: Expose derived consumption, not raw registers

**Status:** Accepted · **Date:** 2026-07-23

## Context

`GET /portal/meters/{id}/energy` returns readings like:

```json
{"timestamp": "24/06/2026 00:00", "kwh": "42594.47", "kvah": "46002.02", "voltR": "230"}
```

Verified across every meter sampled, `kwh` and `kvah` increase monotonically. `J100001` runs
42594.05 → 42732.85 over seven days. These are **cumulative register readings** — odometer
values — not per-interval consumption.

An endpoint named "consumption" that returned these verbatim would invite exactly the wrong
aggregation. Summing the `kwh` column over a week yields ~14,000,000 kWh for a household:
wrong by five orders of magnitude, and entirely plausible-looking in a JSON response.

Two further measured properties matter:

* **Cadence varies.** Most meters sample every 30 minutes (337 points over 7 days);
  `J100400`–`J100402` sample **daily** (8 points).
* **The portal ignores date parameters** — `from`/`to`/`days`/`limit`/`page` all return the
  identical full window.

## Decision

Serve **derived** consumption: each interval is the difference between consecutive register
readings. Sampling cadence is inferred from the data (modal gap between readings). Windowing
and hourly/daily resampling happen in this service. Raw readings remain available via
`include_readings=true`.

A negative delta yields `null`, not a negative number — it means a register rollover or a
meter replacement, and the true consumption is unknowable from two samples.

## Consequences

**Positive.** The obvious client operation — summing `kwh` — is now correct. Daily-cadence
meters are handled without special-casing. An unknown value is `null` and never silently
becomes zero.

**Negative.** The interval count is one fewer than the reading count, which surprises people
until they think about it. Consumption before the first reading in a window is unknowable, so
a narrow window loses its leading interval.

**Mitigation.** The behaviour is pinned by `test_consumption_is_the_delta_not_the_reading`
and `test_a_register_decrease_is_not_negative_consumption`, and the endpoint description
states the derivation explicitly.

## Alternatives considered

**Return registers verbatim and document the caveat.** Rejected: a documented footgun is
still a footgun, and this one produces confidently wrong numbers rather than errors.

**Return both, undifferentiated.** Rejected: forces every client to re-implement the same
differencing, and they will not all get rollover handling right.

**Assume a fixed 30-minute cadence.** Rejected: measurably false for three meters, and the
failure mode is silent mislabelling of every interval rather than a visible error.
