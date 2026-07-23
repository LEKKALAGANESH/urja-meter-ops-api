"""Meter endpoints: listing, detail, consumption and proximity search."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Path, Query, Response

from ...domain import consumption as consumption_ops
from ...domain.models import (
    ConsumptionResponse,
    Meter,
    MeterSummary,
    MeterWithDistance,
    Page,
)
from ...errors import ErrorResponse, NotFoundError, ValidationError
from ...portal.exceptions import PortalNotFound
from ...store.snapshot import (
    SORTABLE_FIELDS,
    MeterQuery,
    Snapshot,
    SnapshotStore,
    meters_near,
    query_meters,
)
from ..deps import Pagination, get_snapshot, get_store, pagination

router = APIRouter(prefix="/meters", tags=["Meters"])


@router.get(
    "",
    response_model=Page[MeterSummary],
    summary="List meters",
    description=(
        "Filter, sort and page the full meter estate.\n\n"
        "Served from the local snapshot, so this supports filters the portal itself "
        "cannot: by make, status, phase, install type, build and any hierarchy code. "
        "All filters are ANDed. `search` matches meter id or serial as a literal "
        "substring - unlike the portal, `%` and `_` are not wildcards here."
    ),
)
async def list_meters(
    response: Response,
    page: Pagination = Depends(pagination),
    snapshot: Snapshot = Depends(get_snapshot),
    search: str | None = Query(None, description="Literal substring of meter id or serial."),
    make: str | None = Query(None, description="Exact manufacturer, e.g. 'HPL'."),
    install_status: str | None = Query(
        None, description="installed | faulty | decommissioned"
    ),
    phase_type: str | None = Query(None, description="single | three"),
    install_type: str | None = Query(None, description="whole_current | ct_operated"),
    build: str | None = Query(None, description="legacy | v2"),
    dt_code: str | None = Query(
        None, description="Distribution transformer code, e.g. 'DT-001'."
    ),
    hierarchy_code: str | None = Query(
        None, description="Any hierarchy code at any level, e.g. 'F-002' or 'Z-01'."
    ),
    has_location: bool | None = Query(
        None, description="Only meters with/without coordinates."
    ),
    with_issues: bool | None = Query(
        None, description="Only meters with/without data-quality flags."
    ),
    sort: str = Query("meter_id", description=f"One of: {', '.join(SORTABLE_FIELDS)}"),
    order: str = Query("asc", pattern="^(asc|desc)$"),
) -> Page[MeterSummary]:
    if sort not in SORTABLE_FIELDS:
        raise ValidationError(
            f"Unknown sort field {sort!r}.",
            details={"allowed": list(SORTABLE_FIELDS)},
        )

    matches = query_meters(
        snapshot.meters,
        MeterQuery(
            search=search,
            make=make,
            install_status=install_status,
            phase_type=phase_type,
            install_type=install_type,
            build=build,
            dt_code=dt_code,
            hierarchy_code=hierarchy_code,
            has_location=has_location,
            with_issues=with_issues,
            sort=sort,
            descending=order == "desc",
        ),
    )
    _tag_snapshot(response, snapshot)
    items = [_summarise(m) for m in page.slice(matches)]
    return page.envelope(items, len(matches))


@router.get(
    "/near",
    response_model=list[MeterWithDistance],
    summary="Find meters near a location",
    description=(
        "Great-circle proximity search over the snapshot - a question the portal cannot "
        "answer at all, since it never exposes coordinates in its listing.\n\n"
        "Declared before `/{meter_id}` so the literal path wins over the parameter."
    ),
)
async def meters_near_point(
    response: Response,
    snapshot: Snapshot = Depends(get_snapshot),
    lat: float = Query(..., ge=-90, le=90, description="Latitude in decimal degrees."),
    lng: float = Query(..., ge=-180, le=180, description="Longitude in decimal degrees."),
    radius_km: float = Query(1.0, gt=0, le=500, description="Search radius in kilometres."),
    limit: int = Query(50, ge=1, le=500),
) -> list[MeterWithDistance]:
    found = meters_near(snapshot.meters, lat, lng, radius_km)[:limit]
    _tag_snapshot(response, snapshot)
    return [
        MeterWithDistance(**_summarise(meter).model_dump(), distance_km=distance)
        for meter, distance in found
    ]


@router.get(
    "/{meter_id}",
    response_model=Meter,
    summary="Get one meter",
    description=(
        "Full record including the normalised root-to-leaf hierarchy path and coordinates.\n\n"
        "Upstream this data has two incompatible encodings depending on the meter's build; "
        "both are normalised to this single shape."
    ),
    responses={404: {"model": ErrorResponse, "description": "No such meter."}},
)
async def get_meter(
    response: Response,
    meter_id: str = Path(..., description="Case-sensitive portal id, e.g. 'J100001'."),
    snapshot: Snapshot = Depends(get_snapshot),
) -> Meter:
    meter = snapshot.meters_by_id.get(meter_id)
    if meter is None:
        raise NotFoundError(
            f"No meter with id {meter_id!r}.",
            details={"hint": "Meter ids are case-sensitive upstream."},
        )
    _tag_snapshot(response, snapshot)
    return meter


@router.get(
    "/{meter_id}/consumption",
    response_model=ConsumptionResponse,
    summary="Get consumption history",
    description=(
        "Energy **consumed**, derived by differencing the portal's cumulative registers.\n\n"
        "The portal returns raw register totals; summing that column would overstate usage "
        "by orders of magnitude. Each interval here is the difference between consecutive "
        "readings. Sampling cadence is inferred from the data (30-minute for most meters, "
        "daily for some), never assumed.\n\n"
        "The upstream series is a fixed ~7-day window and ignores date parameters, so "
        "`from`/`to` are applied locally."
    ),
    responses={404: {"model": ErrorResponse, "description": "No such meter."}},
)
async def get_consumption(
    response: Response,
    meter_id: str = Path(..., description="Case-sensitive portal id."),
    store: SnapshotStore = Depends(get_store),
    snapshot: Snapshot = Depends(get_snapshot),
    start: datetime | None = Query(
        None, alias="from", description="Inclusive lower bound (ISO-8601, naive local time)."
    ),
    end: datetime | None = Query(
        None, alias="to", description="Inclusive upper bound (ISO-8601, naive local time)."
    ),
    granularity: str = Query("raw", description="raw | hourly | daily"),
    include_readings: bool = Query(
        False, description="Also return the raw cumulative register readings."
    ),
) -> ConsumptionResponse:
    if granularity not in consumption_ops.VALID_GRANULARITIES:
        raise ValidationError(
            f"Unknown granularity {granularity!r}.",
            details={"allowed": sorted(consumption_ops.VALID_GRANULARITIES)},
        )
    if start and end and start > end:
        raise ValidationError("'from' must not be after 'to'.")
    if meter_id not in snapshot.meters_by_id:
        raise NotFoundError(f"No meter with id {meter_id!r}.")

    try:
        readings = await store.get_readings(meter_id)
    except PortalNotFound as exc:
        # The snapshot knows the meter but the portal disagrees - a real inconsistency,
        # so report it as not-found rather than as our own failure.
        raise NotFoundError(f"Portal has no consumption series for {meter_id!r}.") from exc

    _tag_snapshot(response, snapshot)
    return consumption_ops.build_response(
        meter_id,
        readings,
        start=start,
        end=end,
        granularity=granularity,
        include_readings=include_readings,
    )


def _summarise(meter: Meter) -> MeterSummary:
    return MeterSummary(
        meter_id=meter.meter_id,
        serial_no=meter.serial_no,
        make=meter.make,
        phase_type=meter.phase_type,
        install_status=meter.install_status,
        dt_code=meter.dt_code,
        location=meter.location,
    )


def _tag_snapshot(response: Response, snapshot: Snapshot) -> None:
    """Advertise data provenance so callers can apply their own freshness policy."""
    response.headers["X-Snapshot-Age-Seconds"] = f"{snapshot.age_seconds():.1f}"
    response.headers["X-Snapshot-Built-At"] = snapshot.built_at.isoformat()
    response.headers["X-Snapshot-Source"] = snapshot.source
