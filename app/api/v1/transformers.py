"""Distribution transformer endpoints, backed by the portal's authoritative DT registry."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query, Response

from ...domain.models import MeterSummary, Page, Transformer
from ...errors import ErrorResponse, NotFoundError
from ...store.snapshot import Snapshot
from ..deps import Pagination, get_snapshot, pagination

router = APIRouter(prefix="/transformers", tags=["Transformers"])


@router.get(
    "",
    response_model=Page[Transformer],
    summary="List distribution transformers",
    description=(
        "The DT registry, enriched with a live meter count from the snapshot.\n\n"
        "This registry is treated as the **authoritative** source for DT names: some "
        "meter records carry a stale alias (`DT-007` appears as both 'Sanganer DT 7' and "
        "'Old Malviya Nagar Xfmr'), and the registry value wins. The conflict is still "
        "reported at `/data-quality` rather than hidden."
    ),
)
async def list_transformers(
    response: Response,
    snapshot: Snapshot = Depends(get_snapshot),
    page: Pagination = Depends(pagination),
    feeder_code: str | None = Query(
        None, description="Filter by parent feeder, e.g. 'F-002'."
    ),
    search: str | None = Query(None, description="Literal substring of code or name."),
) -> Page[Transformer]:
    matches = list(snapshot.transformers)
    if feeder_code:
        wanted = feeder_code.strip().lower()
        matches = [t for t in matches if (t.feeder_code or "").lower() == wanted]
    if search:
        needle = search.strip().casefold()
        matches = [
            t
            for t in matches
            if needle in t.code.casefold() or (t.name and needle in t.name.casefold())
        ]
    matches.sort(key=lambda t: t.code)
    _tag(response, snapshot)
    return page.envelope(page.slice(matches), len(matches))


@router.get(
    "/{code}",
    response_model=Transformer,
    summary="Get one transformer",
    responses={404: {"model": ErrorResponse, "description": "No such transformer."}},
)
async def get_transformer(
    response: Response,
    code: str = Path(..., description="Transformer code, e.g. 'DT-001'."),
    snapshot: Snapshot = Depends(get_snapshot),
) -> Transformer:
    transformer = snapshot.transformers_by_code.get(code)
    if transformer is None:
        raise NotFoundError(f"No transformer with code {code!r}.")
    _tag(response, snapshot)
    return transformer


@router.get(
    "/{code}/meters",
    response_model=Page[MeterSummary],
    summary="List meters on a transformer",
    responses={404: {"model": ErrorResponse, "description": "No such transformer."}},
)
async def get_transformer_meters(
    response: Response,
    code: str = Path(...),
    snapshot: Snapshot = Depends(get_snapshot),
    page: Pagination = Depends(pagination),
) -> Page[MeterSummary]:
    if code not in snapshot.transformers_by_code:
        raise NotFoundError(f"No transformer with code {code!r}.")
    matches = [m for m in snapshot.meters if m.dt_code == code]
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


def _tag(response: Response, snapshot: Snapshot) -> None:
    response.headers["X-Snapshot-Age-Seconds"] = f"{snapshot.age_seconds():.1f}"
    response.headers["X-Snapshot-Source"] = snapshot.source
