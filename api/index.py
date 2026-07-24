"""Vercel serverless entrypoint.

Vercel's Python runtime serves the ASGI ``app`` exposed here - the *same* application object
the container runs via ``uvicorn app.main:app``, so there is one app, not a fork.

``attach_runtime`` is called eagerly at import because serverless platforms do not reliably
run ASGI lifespan events (see DEPLOY-VERCEL.md); without it ``app.state.store`` would be
missing and every request would 503. The snapshot is still built lazily on the first request.

Serverless trade-off: each cold start is a fresh process with an empty snapshot, so the first
request after a cold start rebuilds it (one portal export). A persistent container host keeps
the snapshot warm and remains the recommended target.
"""

from __future__ import annotations

import os
import sys

# Make the project root importable regardless of Vercel's working directory, before the
# first-party import below.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app, attach_runtime

attach_runtime(app)

__all__ = ["app"]
