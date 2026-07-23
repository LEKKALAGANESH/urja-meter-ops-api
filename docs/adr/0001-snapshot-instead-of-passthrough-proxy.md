# ADR-0001: Maintain a local snapshot instead of proxying each request

**Status:** Accepted · **Date:** 2026-07-23

## Context

The obvious design for an API over a legacy portal is a pass-through proxy: translate each
inbound request into one or more upstream calls, normalise the response, return it.

Reconnaissance changed the calculation. The portal exposes `GET /portal/export` (HMAC-signed),
which returns **all 403 meters in a single request** — and its records are strictly richer
than the paginated listing, carrying the full hierarchy path and float coordinates that
`/portal/meters/search` omits entirely.

Meanwhile the portal's own query capability is very limited:

* search matches meter id and serial only — not make, status, phase or transformer
* no sorting, no multi-attribute filtering
* no hierarchy endpoint at all
* no coordinates in any listing
* pagination is unvalidated (`page=0` silently returns page 1)

## Decision

Fetch the whole estate via the bulk export, hold it in memory as an immutable snapshot with
a TTL, and answer queries locally. Per-meter time series are still fetched live, because they
are not included in the export.

## Consequences

**Positive.** Filtering, sorting, hierarchy reconstruction and proximity search become
possible — none of them can be delegated upstream. Query latency is sub-millisecond and
independent of portal latency (~350 ms/request). The portal sees one request per TTL rather
than one per inbound call. A portal outage degrades to stale data rather than total failure.

**Negative.** Data can be up to `SNAPSHOT_TTL_SECONDS` stale; every response carries
`X-Snapshot-Age-Seconds` so callers can apply their own policy. Memory scales with estate
size. Each instance holds its own copy, so instances can briefly disagree.

**Risk.** If `/portal/export` is withdrawn or its signing changes, the primary path breaks.
Mitigated by a fallback that crawls the paginated search — which yields a materially poorer
record (no hierarchy, no geo), so it is only accepted on a **cold start**. Replacing a
complete stale snapshot with a degraded fresh one would be a downgrade, not a recovery.

## Alternatives considered

**Pass-through proxy.** Rejected: inherits every portal limitation, adds a network hop to
each call, and makes the hierarchy and proximity features impossible.

**Snapshot in Postgres/PostGIS.** Rejected *for now*: the entire dataset is ~400 KB. A
database would add a container, migrations and operational burden to a service whose data
fits in a rounding error of RAM. This becomes the right answer at roughly 500k meters — see
README *Scaling*.

**Cache individual responses.** Rejected: caches responses rather than data, so it still
cannot answer questions the portal itself cannot answer.
