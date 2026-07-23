# ADR-0004: Surface upstream data-quality issues through the API

**Status:** Accepted · **Date:** 2026-07-23

## Context

The upstream data has real defects, all measured:

* 22 meters carry a hierarchy rung with a blank `code` or blank `name`
* `DT-007` is named `"Sanganer DT 7"` on 10 meters and `"Old Malviya Nagar Xfmr"` on 3
* mid-level hierarchy codes are not globally unique (see ADR-0003)
* meter detail arrives in two incompatible encodings depending on the meter's `build`

A wrapper has a choice here: hide all of this behind a tidy façade, or expose it. Hiding it
is how downstream teams end up debugging *our* service for a fault that was always upstream.

## Decision

Normalise aggressively — one canonical shape, real types, explicit nulls — but **never
silently discard information.** Every anomaly detected during normalisation attaches a
machine-readable slug to the affected meter, and those slugs are aggregated at
`GET /api/v1/data-quality` with counts and sample meter ids.

Blanks are repaired only where the dataset resolves them **unambiguously** (exactly one
candidate elsewhere in the data). Where two or more candidates exist, the blank is preserved
and counted as unrepairable. Repairs are themselves flagged, so a repaired value stays
distinguishable from an original one.

## Consequences

**Positive.** Consumers can filter to clean records (`with_issues=false`) or deliberately
investigate dirty ones. The endpoint doubles as **drift detection**: if the portal's data
changes, the counts move. Debugging conversations start from evidence rather than
speculation.

**Negative.** The API openly admits its data is imperfect, which is less impressive at a
glance and considerably more useful in practice. The slugs are an extra vocabulary for
clients to learn, though they can be ignored entirely.

## Alternatives considered

**Silently drop bad records.** Rejected: a missing meter is indistinguishable from a meter
that does not exist, which is the worst possible failure mode for an inventory system.

**Fail the whole snapshot on any anomaly.** Rejected: 22 imperfect records out of 403 would
take down a service that is otherwise entirely usable.

**Repair everything with a best guess.** Rejected: filling a blank from one unambiguous
candidate is inference; filling it when several exist is fabrication — and fabricated
structure is indistinguishable from real structure once it is in the response.

**Log anomalies only.** Rejected: logs are invisible to API consumers, who are exactly the
people who need to know.
