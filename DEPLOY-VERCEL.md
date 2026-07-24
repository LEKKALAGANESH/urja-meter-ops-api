# Deploying to Vercel

The repo is configured for Vercel's canonical FastAPI setup - Vercel serves the `app`
instance in `app/main.py` directly (no shim entrypoint):

- **`pyproject.toml`** → `[tool.vercel] entrypoint = "app.main:app"` names the ASGI app
  explicitly, so the build never guesses between candidate entrypoints.
- **`vercel.json`** → a `functions` block for `app/main.py` with `maxDuration: 60` (headroom
  for a cold-start snapshot build) and `excludeFiles` to keep tests/video out of the bundle.
  No `rewrites` - Vercel routes the whole app automatically.
- **`.python-version`** → `3.12` (pins the runtime; the app is tested on 3.11 and 3.12).
- **Lifespan-independent state:** the portal client/session/snapshot store attach lazily on
  the first request (`app/api/deps.py::_ensure_runtime`), so the app works even when Vercel
  does not run ASGI lifespan events (a
  [documented](https://community.vercel.com/t/python-fastapi-state-not-exist-on-vercel-but-does-locally-lifespan/2609)
  edge case) - no import-time work that could crash the function.

## Deploy

1. **Import the repo** at <https://vercel.com/new>. Root Directory = the repo root (where
   `requirements.txt` and `vercel.json` live). Vercel auto-detects the Python runtime.
2. **Set environment variables** (Project → Settings → Environment Variables):

   | Variable | Required | Notes |
   |---|---|---|
   | `PORTAL_USERNAME` | yes | Portal login |
   | `PORTAL_PASSWORD` | yes | Portal password |
   | `SNAPSHOT_REFRESH_ON_START` | **recommended** | Set to `false` on serverless — skips the portal export at cold start, so startup does zero network I/O; the snapshot builds on the first request instead. |
   | `LOG_FORMAT` | no | `json` (default) |
   | any other `Settings` field | no | see `.env.example` |

3. **Deploy.** Then:
   - `https://<deployment>/` redirects a browser to the console at `/app/`.
   - `https://<deployment>/docs` serves the API docs.
   - `https://<deployment>/api/v1/health/live` returns `{"status":"alive",...}`.

## Verify (under 2 minutes)

```bash
curl -s https://<deployment>/api/v1/health/live      # {"status":"alive",...}
curl -s https://<deployment>/api/v1/meters | head -c 200   # real data once creds are set
```

## Serverless trade-offs (why a container host is still recommended)

This service keeps an **in-memory snapshot** of the estate, refreshed on a TTL, and uses
per-process locks for single-flight refresh and the amplification throttle. Vercel runs
**stateless, ephemeral functions**, so:

- Each **cold start** is a fresh process with an empty snapshot; the first request after it
  rebuilds the snapshot (one portal export) and is slower.
- The **single-flight lock** and **refresh throttle** are per-instance, so concurrent
  instances do not share them - the guarantees hold within an instance, not across the fleet.
- There is no long-lived process to run the **proactive session refresh** or a background
  TTL rebuild; work happens on request.

For production, prefer a **persistent container host** - the existing `Dockerfile` runs
anywhere: Render, Railway, Fly.io, Google Cloud Run (`--min-instances=1` to keep the snapshot
warm), AWS ECS/Fargate, Azure Container Apps. Making Vercel a *good* fit means externalising
the snapshot to a shared store (Vercel KV / Upstash Redis) plus a Vercel Cron refresh - the
same scaling path the README documents under *Scaling*.
