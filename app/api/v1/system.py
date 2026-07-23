"""Operational endpoints: health, readiness, snapshot control, data quality, statistics."""

from __future__ import annotations

from collections import Counter
from typing import Any

from fastapi import APIRouter, Depends, Response

from ...config import Settings
from ...domain.models import DataQualityReport, SnapshotInfo
from ...errors import ErrorResponse
from ...portal.exceptions import PortalError
from ...portal.session import PortalSession
from ...store.snapshot import Snapshot, SnapshotStore
from ..deps import get_config, get_session, get_snapshot, get_store

router = APIRouter(tags=["System"])


@router.get(
    "/health/live",
    summary="Liveness probe",
    description=(
        "Is the process up? Never touches the portal.\n\n"
        "Kept independent of upstream deliberately: if this depended on the portal, a "
        "portal outage would make an orchestrator kill and restart healthy pods, turning "
        "a degradation into an outage."
    ),
)
async def liveness(config: Settings = Depends(get_config)) -> dict[str, Any]:
    return {"status": "alive", "service": config.app_name, "version": config.app_version}


@router.get(
    "/health/ready",
    summary="Readiness probe",
    description=(
        "Can this instance serve real traffic? Verifies the portal session is genuinely "
        "accepted upstream and that a snapshot exists.\n\n"
        "Returns 503 when not ready, so a load balancer drains this instance instead of "
        "serving errors. A *stale* snapshot still counts as ready - degraded is better "
        "than down."
    ),
    responses={503: {"model": ErrorResponse, "description": "Not ready to serve traffic."}},
)
async def readiness(
    response: Response,
    store: SnapshotStore = Depends(get_store),
    session: PortalSession = Depends(get_session),
) -> dict[str, Any]:
    checks: dict[str, Any] = {}

    try:
        remote = await session.fetch_remote_session()
        checks["portal_session"] = {
            "ok": remote is not None,
            "expires_at": session.expires_at.isoformat() if session.expires_at else None,
            "login_count": session.login_count,
        }
    except PortalError as exc:
        checks["portal_session"] = {"ok": False, "error": str(exc)}

    info = store.info()
    checks["snapshot"] = {
        "ok": info.meter_count > 0,
        "age_seconds": info.age_seconds,
        "is_stale": info.is_stale,
        "meter_count": info.meter_count,
        "last_error": info.last_error,
    }

    ready = all(check.get("ok") for check in checks.values())
    if not ready:
        response.status_code = 503
    return {"status": "ready" if ready else "not_ready", "checks": checks}


@router.get(
    "/system/snapshot",
    response_model=SnapshotInfo,
    summary="Snapshot provenance",
    description=(
        "Age, size, source and refresh history of the local index. Lets a caller decide "
        "for itself whether the data is fresh enough for its purpose."
    ),
)
async def snapshot_info(store: SnapshotStore = Depends(get_store)) -> SnapshotInfo:
    return store.info()


@router.post(
    "/system/snapshot/refresh",
    response_model=SnapshotInfo,
    summary="Force a snapshot refresh",
    description=(
        "Rebuild the index immediately, ignoring the TTL.\n\n"
        "Deliberately unauthenticated in this build because the service has no auth model "
        "at all (see README, *What I intentionally left out*). It is idempotent and "
        "read-only with respect to the portal, but it does cost one upstream export, so "
        "it should sit behind auth and a rate limit before any real deployment."
    ),
)
async def refresh_snapshot(store: SnapshotStore = Depends(get_store)) -> SnapshotInfo:
    await store.get(force_refresh=True)
    return store.info()


@router.get(
    "/system/session",
    summary="Portal session status",
    description="Our view of the upstream session, for debugging re-authentication behaviour.",
)
async def session_status(session: PortalSession = Depends(get_session)) -> dict[str, Any]:
    return {
        "authenticated": session.is_authenticated,
        "expires_at": session.expires_at.isoformat() if session.expires_at else None,
        "authenticated_at": (
            session.authenticated_at.isoformat() if session.authenticated_at else None
        ),
        "login_count": session.login_count,
    }


@router.get(
    "/data-quality",
    response_model=DataQualityReport,
    summary="Data-quality report",
    description=(
        "Every inconsistency detected in the upstream data, aggregated with counts and "
        "sample meter ids.\n\n"
        "This exists because hiding upstream mess behind a tidy API is how downstream "
        "teams end up debugging the wrong system. It also doubles as drift detection: if "
        "the portal's data changes, these counts move."
    ),
)
async def data_quality(snapshot: Snapshot = Depends(get_snapshot)) -> DataQualityReport:
    return snapshot.quality


@router.get(
    "/stats",
    summary="Estate summary",
    description=(
        "Aggregate counts across the snapshot - the overview the portal never renders."
    ),
)
async def stats(snapshot: Snapshot = Depends(get_snapshot)) -> dict[str, Any]:
    def tally(values) -> dict[str, int]:
        return dict(sorted(Counter(v for v in values if v).items()))

    return {
        "meter_count": len(snapshot.meters),
        "transformer_count": len(snapshot.transformers),
        "snapshot_built_at": snapshot.built_at.isoformat(),
        "by_install_status": tally(
            m.install_status.value if m.install_status else None for m in snapshot.meters
        ),
        "by_make": tally(m.make for m in snapshot.meters),
        "by_phase_type": tally(
            m.phase_type.value if m.phase_type else None for m in snapshot.meters
        ),
        "by_build": tally(m.build.value if m.build else None for m in snapshot.meters),
        "with_location": sum(1 for m in snapshot.meters if m.location),
        "with_data_quality_issues": sum(1 for m in snapshot.meters if m.data_quality),
        "hierarchy_nodes": _count_nodes(snapshot.tree) - 1,  # exclude synthetic root
    }


def _count_nodes(node) -> int:
    return 1 + sum(_count_nodes(child) for child in node.children)
