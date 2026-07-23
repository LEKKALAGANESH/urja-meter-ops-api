# Runbook — Urja Meter Ops API

Operational procedures for running this service. Written for whoever is on call, not for
whoever wrote it.

---

## What this service is

A read-only REST API in front of the legacy **Urja Meter Ops** portal. It authenticates to
the portal as a normal user, pulls the whole meter estate via the portal's signed bulk
export, holds it in memory as a snapshot, and serves queries locally. Per-meter consumption
is fetched live.

**It has no database and no write path.** Losing all state costs one portal export
(~600 ms). That shapes every procedure below: recovery is almost always "restart it".

---

## Deploy

```bash
docker build -t flock-energy-api:$(git rev-parse --short HEAD) .
docker run -d -p 8000:8000 --env-file .env flock-energy-api:<tag>
```

Or `docker compose up -d`. Required env: `PORTAL_USERNAME`, `PORTAL_PASSWORD`. Everything
else has a working default — see `.env.example`.

**Verify a deploy is good:**

```bash
curl -fsS localhost:8000/api/v1/health/ready | jq .
# expect: status "ready", checks.portal_session.ok true, checks.snapshot.meter_count > 0
```

Startup takes ~1 s: authenticate, fetch export, build index. The service **starts even if
the portal is unreachable** — deliberately, so it can be inspected during an upstream
outage. It will report `not_ready` until the portal returns.

## Rollback

The service is stateless. Roll back by redeploying the previous image tag; no migration, no
data repair, nothing to undo upstream.

---

## Health semantics

| Endpoint | Meaning | Orchestrator action |
|---|---|---|
| `GET /api/v1/health/live` | Process is up. **Never touches the portal.** | Restart the container if this fails |
| `GET /api/v1/health/ready` | Portal session valid **and** a snapshot exists | Remove from load balancer if 503 |

Liveness is deliberately independent of the portal. If it depended on upstream, a portal
outage would make the orchestrator kill healthy containers and turn a degradation into an
outage. **Never "fix" this by adding a portal check to liveness.**

A *stale* snapshot still counts as ready. Degraded is better than down.

---

## Diagnosis

Every log line is JSON and carries `request_id`, which is also returned in the
`X-Request-ID` response header. Start from a failing request's ID:

```bash
docker logs <container> | jq 'select(.request_id=="<id>")'
```

Ask the service what it thinks of itself:

```bash
curl -s localhost:8000/api/v1/system/snapshot | jq .   # age, source, refresh count, last error
curl -s localhost:8000/api/v1/system/session  | jq .   # expiry, login count
```

Every data response also carries `X-Snapshot-Age-Seconds`, `X-Snapshot-Built-At` and
`X-Snapshot-Source`.

---

## Known failure modes

### 1. `/health/ready` returns 503, `portal_session.ok: false`

Portal is down, credentials changed, or the session was revoked.

* Check `system/session` → `login_count`. Climbing fast means sessions are being
  invalidated faster than expected.
* Check logs for `PortalAuthError` (credentials) vs `PortalUnavailable` (network).
* Credentials rotate → update `.env` and restart. **Nothing else fixes an auth failure.**

### 2. Responses succeed but `X-Snapshot-Age-Seconds` keeps growing

Refreshes are failing while stale data is still served — the intended degradation.

```bash
curl -s localhost:8000/api/v1/system/snapshot | jq .last_error
```

The service will recover on its own when the portal does. Force a rebuild with
`POST /api/v1/system/snapshot/refresh`. **Do not restart in a loop** — see #3.

### 3. `503 upstream_rate_limited` with a `Retry-After` header

The portal rate limits **login** (measured: the 4th sign-in in a window returns 429 with
`x-retry-after: 10`). Data routes showed no limit at ~3 req/s.

Restart loops are the usual cause: each restart is a fresh login. **Stop restarting**, wait
the advertised interval, let the backoff work. If it persists, raise
`SESSION_REFRESH_MARGIN_SECONDS` so refreshes happen further from the boundary.

### 4. `502 upstream_error` — "data in an unexpected shape"

The portal changed. This is the one failure that needs an engineer, not an operator.

```bash
make test-live     # 18 contract tests; the failing one names exactly what moved
```

Consult `PROTOCOL.md`, then fix the adapter in `app/portal/`.

### 5. Memory growth

Snapshot is ~280 KB at 403 meters (~710 bytes/meter); the consumption cache is LRU-bounded
at `CONSUMPTION_CACHE_MAX_ENTRIES` (default 256). Sustained growth beyond a few tens of MB
is a bug, not load — capture a heap snapshot before restarting.

---

## Tuning

| Symptom | Setting | Direction |
|---|---|---|
| Data too stale | `SNAPSHOT_TTL_SECONDS` | lower (costs one export per TTL) |
| Too much upstream load | `SNAPSHOT_TTL_SECONDS` | raise |
| Portal complaining about volume | `PORTAL_MAX_CONCURRENCY` | lower (default 6) |
| Sessions expiring mid-request | `SESSION_REFRESH_MARGIN_SECONDS` | raise (default 120) |
| Consumption endpoints slow | `CONSUMPTION_CACHE_TTL_SECONDS` | raise (default 120) |

---

## Escalate when

* `make test-live` fails → the portal's contract changed; needs an engineer.
* `login_count` grows without restarts → sessions being revoked upstream; talk to the portal team.
* Data-quality counts move sharply (`GET /api/v1/data-quality`) → upstream data changed;
  the counts are deliberately a drift detector.

## Do not

* Add a portal check to `/health/live`.
* Restart repeatedly to clear a 429 — it makes it worse.
* Expose `POST /system/snapshot/refresh` publicly. **This service has no authentication of
  its own** (see README, *What I intentionally left out*); put it behind a gateway before
  any deployment that is reachable from outside a trusted network.
