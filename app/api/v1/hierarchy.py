"""Network hierarchy endpoints - the reconstructed tree the portal never exposes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Response

from ...domain.hierarchy import find_node, meter_path, prune_depth
from ...domain.models import HierarchyNode, MeterSummary, Page
from ...errors import ErrorResponse, NotFoundError
from ...store.snapshot import Snapshot
from ..deps import Pagination, get_snapshot, pagination

router = APIRouter(prefix="/hierarchy", tags=["Hierarchy"])

_TREE_DESCRIPTION = (
    "The utility's network tree (Zone -> Circle -> Division -> Subdivision -> Substation "
    "-> Feeder -> DT), reconstructed by folding together every meter's root-to-leaf path. "
    "The portal only ever exposes one meter's path at a time.\n\n"
    "**Node identity is the full ancestor path, not the bare code.** Mid-level codes are "
    "not globally unique upstream - `D-01` occurs under three different circles - so "
    "`Z-01/C-01/D-01` and `Z-01/C-03/D-01` are distinct nodes. Collapsing them by code "
    "would silently merge unrelated parts of the network. See PROTOCOL.md.\n\n"
    "`meter_count` is cumulative: it counts meters anywhere beneath the node."
)


@router.get(
    "",
    response_model=HierarchyNode,
    summary="Get the network tree",
    description=_TREE_DESCRIPTION,
)
async def get_hierarchy(
    response: Response,
    snapshot: Snapshot = Depends(get_snapshot),
    depth: int = Query(
        7,
        ge=0,
        le=7,
        description="Levels to return below the root. 0 returns the root only.",
    ),
) -> HierarchyNode:
    _tag(response, snapshot)
    return prune_depth(snapshot.tree, depth)


@router.get(
    "/nodes/{node_id:path}/meters",
    response_model=Page[MeterSummary],
    summary="List meters under a subtree",
    description="Every meter whose path passes through the given node, paginated.",
    responses={404: {"model": ErrorResponse, "description": "No node with that path id."}},
)
async def get_node_meters(
    response: Response,
    node_id: str = Path(..., description="Path id, e.g. 'Z-01/C-01'."),
    snapshot: Snapshot = Depends(get_snapshot),
    page: Pagination = Depends(pagination),
) -> Page[MeterSummary]:
    if find_node(snapshot.tree, node_id) is None:
        raise NotFoundError(f"No hierarchy node with id {node_id!r}.")

    prefix = "" if node_id == "network" else node_id
    matches = [m for m in snapshot.meters if meter_path(m).startswith(prefix)]
    _tag(response, snapshot)
    items = [
        MeterSummary(
            meter_id=m.meter_id,
            serial_no=m.serial_no,
            make=m.make,
            phase_type=m.phase_type,
            install_status=m.install_status,
            dt_code=m.dt_code,
            location=m.location,
        )
        for m in page.slice(matches)
    ]
    return page.envelope(items, len(matches))


# Registered *after* the '/meters' sub-resource above: the ':path' converter is greedy, so
# if this route came first it would match '/nodes/Z-01/meters' with node_id='Z-01/meters'
# and 404. Ordering is the fix, and this comment is why it must not be "tidied".
@router.get(
    "/nodes/{node_id:path}",
    response_model=HierarchyNode,
    summary="Get a subtree",
    description=(
        "Fetch one node and its descendants by path id, e.g. `Z-01/C-01/D-01`. "
        "Use this to drill into a branch without transferring the whole tree."
    ),
    responses={404: {"model": ErrorResponse, "description": "No node with that path id."}},
)
async def get_hierarchy_node(
    response: Response,
    node_id: str = Path(..., description="Path id, e.g. 'Z-01/C-01' or 'network'."),
    snapshot: Snapshot = Depends(get_snapshot),
    depth: int = Query(7, ge=0, le=7),
) -> HierarchyNode:
    node = find_node(snapshot.tree, node_id)
    if node is None:
        raise NotFoundError(
            f"No hierarchy node with id {node_id!r}.",
            details={"hint": "Node ids are '/'-joined code paths, e.g. 'Z-01/C-01/D-01'."},
        )
    _tag(response, snapshot)
    return prune_depth(node, depth)


def _tag(response: Response, snapshot: Snapshot) -> None:
    response.headers["X-Snapshot-Age-Seconds"] = f"{snapshot.age_seconds():.1f}"
    response.headers["X-Snapshot-Source"] = snapshot.source
