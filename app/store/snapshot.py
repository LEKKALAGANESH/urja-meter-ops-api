"""In-memory snapshot of the portal's meter estate, plus the query layer over it.

**Why a snapshot exists.** The portal cannot answer the questions people actually have. It
offers substring search over meter number and serial only - no filter by make, status,
phase or transformer; no sorting; no hierarchy; no "what is near this location". Proxying
each API call straight through would inherit all of those limitations *and* add a network
hop.

The bulk export changes the economics: **403 meters in one signed request**, complete with
hierarchy and coordinates. Holding that in memory costs roughly 400 KB and makes every
one of those queries a local operation.

**Freshness.** The snapshot carries a TTL (default 300 s). Reads are served from it while
fresh; the first read after expiry triggers a rebuild. Refreshes are single-flight, and a
failed refresh **keeps serving the previous snapshot** rather than erroring - stale data
with an honest ``age_seconds`` beats no data. Every response advertises the snapshot's age
via ``X-Snapshot-Age-Seconds``, so a caller can apply its own freshness policy.

**Where this breaks.** Single-process memory, rebuilt in full. That is right for 403 meters
and wrong by roughly two orders of magnitude - the scaling analysis and what to change is
in README.md under *Scaling*.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from ..domain import hierarchy as hierarchy_ops
from ..domain import normalize
from ..domain.models import (
    DataQualityReport,
    HierarchyLevel,
    HierarchyNode,
    Meter,
    MeterReading,
    SnapshotInfo,
    Transformer,
)
from ..domain.quality import build_report
from ..portal.client import UrjaPortalClient
from ..portal.exceptions import PortalError

logger = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True)
class Snapshot:
    """An immutable point-in-time view of the estate.

    Immutable by construction: a refresh publishes a whole new object by atomic rebind, so
    an in-flight request can never observe a half-rebuilt index.
    """

    built_at: datetime
    source: str
    meters: tuple[Meter, ...]
    meters_by_id: dict[str, Meter]
    transformers: tuple[Transformer, ...]
    transformers_by_code: dict[str, Transformer]
    tree: HierarchyNode
    quality: DataQualityReport
    build_duration_ms: float

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or _utcnow()) - self.built_at).total_seconds()


@dataclass
class _CacheEntry:
    readings: tuple[MeterReading, ...]
    stored_at: float


class SnapshotStore:
    """Builds, caches and queries the meter snapshot."""

    def __init__(
        self,
        client: UrjaPortalClient,
        *,
        ttl_seconds: int = 300,
        consumption_ttl_seconds: int = 120,
        consumption_max_entries: int = 256,
        refresh_min_interval_seconds: int = 0,
    ) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._snapshot: Snapshot | None = None
        self._lock = asyncio.Lock()
        self._refresh_count = 0
        self._last_error: str | None = None

        # Minimum spacing between *forced* refreshes (the unauthenticated refresh endpoint).
        # 0 disables it. Monotonic so a wall-clock change can never widen or collapse it.
        # Tracks forced refreshes only, so TTL-driven rebuilds never block a manual one.
        self._refresh_min_interval = refresh_min_interval_seconds
        self._last_forced_refresh_monotonic: float | None = None

        # Per-meter time series are fetched live (they are not in the export) and cached
        # briefly with LRU eviction, so a dashboard polling one meter does not hammer the
        # portal while a 403-meter crawl cannot evict the world.
        self._consumption_ttl = consumption_ttl_seconds
        self._consumption_max = consumption_max_entries
        self._consumption: OrderedDict[str, _CacheEntry] = OrderedDict()

    # --- lifecycle --------------------------------------------------------

    @property
    def current(self) -> Snapshot | None:
        return self._snapshot

    def is_stale(self) -> bool:
        if self._snapshot is None:
            return True
        return self._snapshot.age_seconds() >= self._ttl

    async def get(self, *, force_refresh: bool = False) -> Snapshot:
        """Return a usable snapshot, refreshing if stale.

        Raises:
            PortalError: only if there is no previous snapshot to fall back on.
        """
        if not force_refresh and self._snapshot is not None and not self.is_stale():
            return self._snapshot

        async with self._lock:
            # Another coroutine may have rebuilt while we waited for the lock.
            if not force_refresh and self._snapshot is not None and not self.is_stale():
                return self._snapshot
            return await self._rebuild_under_lock()

    async def _rebuild_under_lock(self) -> Snapshot:
        """Rebuild the snapshot. **Caller must hold ``self._lock``.**

        A failed rebuild keeps the previous snapshot (if any) and records the error rather
        than raising - stale-but-honest beats down. Propagates only when there is nothing
        to fall back on.
        """
        try:
            # Only accept the degraded search-crawl fallback when we have nothing at
            # all. Replacing a complete (if stale) snapshot with one that has no
            # hierarchy and no coordinates would be a downgrade, not a recovery.
            self._snapshot = await self._build(allow_degraded_fallback=self._snapshot is None)
            self._refresh_count += 1
            self._last_error = None
        except PortalError as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            if self._snapshot is None:
                raise
            logger.error(
                "snapshot refresh failed; continuing to serve stale data",
                extra={
                    "error": self._last_error,
                    "stale_age_seconds": self._snapshot.age_seconds(),
                },
            )
        return self._snapshot

    @property
    def refresh_min_interval_seconds(self) -> int:
        return self._refresh_min_interval

    async def refresh_now(self) -> tuple[Snapshot | None, bool]:
        """Forced refresh that honours the minimum interval between forced refreshes.

        The refresh endpoint is unauthenticated (the service has no auth model), so without
        this a caller could force one portal export per request and amplify load on the
        legacy portal without bound. When a forced refresh occurred within
        ``refresh_min_interval_seconds``, this coalesces: it returns the current snapshot
        (which may be ``None`` if none has been built yet) and reports ``refreshed=False``
        rather than hitting the portal again, capping the forced-export rate at one per
        interval regardless of caller volume - and regardless of whether a snapshot exists.

        The interval check and the rebuild share the store lock, so a concurrent burst
        rebuilds exactly once - the first through the gate wins and the rest coalesce.

        Returns:
            ``(snapshot, refreshed)`` - ``refreshed`` is False when the call was coalesced.
        """
        async with self._lock:
            last = self._last_forced_refresh_monotonic
            if (
                self._refresh_min_interval > 0
                and last is not None
                and (time.monotonic() - last) < self._refresh_min_interval
            ):
                return self._snapshot, False
            # Stamp the marker *before* the rebuild, not after: a forced refresh reaches the
            # portal whether or not it succeeds, so even a failed attempt during a cold
            # outage (no snapshot yet) must space out the next one. Setting it only on
            # success left the endpoint unbounded in exactly the state - portal already
            # struggling - where bounding matters most.
            self._last_forced_refresh_monotonic = time.monotonic()
            snapshot = await self._rebuild_under_lock()
            return snapshot, True

    async def _build(self, *, allow_degraded_fallback: bool = True) -> Snapshot:
        """Fetch, normalise, repair and index the estate.

        Primary path is the signed bulk export. If it fails, we fall back to crawling the
        paginated search - but only when ``allow_degraded_fallback`` is set, i.e. when
        there is no existing snapshot. The search rows carry no hierarchy and no
        coordinates, so accepting them while a complete snapshot is already in hand would
        lose data rather than refresh it.
        """
        started = time.perf_counter()

        transformers = await self._load_transformers()
        raw_meters, source = await self._load_meters(allow_degraded_fallback)

        meters: list[Meter] = []
        for row in raw_meters:
            try:
                meters.append(
                    normalize.meter_from_export(row)
                    if source == "export"
                    else normalize.meter_from_search_row(row)
                )
            except ValueError as exc:
                # One malformed row must not sink the whole snapshot.
                logger.warning("skipping unusable meter row", extra={"error": str(exc)})

        repaired, repair_report = hierarchy_ops.repair_blanks(meters, transformers)
        counts = _meter_counts_by_dt(repaired)
        transformers = [
            t.model_copy(update={"meter_count": counts.get(t.code, 0)}) for t in transformers
        ]

        duration_ms = (time.perf_counter() - started) * 1000
        snapshot = Snapshot(
            built_at=_utcnow(),
            source=source,
            meters=tuple(repaired),
            meters_by_id={m.meter_id: m for m in repaired},
            transformers=tuple(transformers),
            transformers_by_code={t.code: t for t in transformers},
            tree=hierarchy_ops.build_tree(repaired),
            quality=build_report(repaired, repair_report),
            build_duration_ms=round(duration_ms, 2),
        )
        logger.info(
            "snapshot built",
            extra={
                "meters": len(repaired),
                "transformers": len(transformers),
                "source": source,
                "duration_ms": snapshot.build_duration_ms,
            },
        )
        return snapshot

    async def _load_meters(self, allow_degraded_fallback: bool) -> tuple[list[dict], str]:
        try:
            return await self._client.export_all_meters(), "export"
        except PortalError as exc:
            if not allow_degraded_fallback:
                # Let this propagate: `get` will keep serving the existing snapshot.
                logger.warning(
                    "bulk export failed; keeping the existing snapshot rather than "
                    "downgrading to a search crawl",
                    extra={"error": str(exc)},
                )
                raise
            logger.warning(
                "bulk export unavailable; falling back to paginated search",
                extra={"error": str(exc)},
            )
        rows: list[dict] = []
        page = 1
        first, total = await self._client.search_meters(page=1)
        rows.extend(first)
        while len(rows) < total:
            page += 1
            batch, _ = await self._client.search_meters(page=page)
            if not batch:
                break
            rows.extend(batch)
        return rows, "search"

    async def _load_transformers(self) -> list[Transformer]:
        rows = await self._client.list_all_dts()
        return [t for t in (normalize.transformer_from_row(r) for r in rows) if t]

    # --- per-meter time series -------------------------------------------

    async def get_readings(self, meter_id: str) -> list[MeterReading]:
        """Fetch (and briefly cache) one meter's interval readings."""
        entry = self._consumption.get(meter_id)
        if entry is not None and (time.monotonic() - entry.stored_at) < self._consumption_ttl:
            self._consumption.move_to_end(meter_id)
            return list(entry.readings)

        rows = await self._client.get_energy_series(meter_id)
        readings = [r for r in (normalize.reading_from_row(row) for row in rows) if r]
        if len(readings) != len(rows):
            logger.warning(
                "dropped readings with unparseable timestamps",
                extra={"meter_id": meter_id, "dropped": len(rows) - len(readings)},
            )

        self._consumption[meter_id] = _CacheEntry(tuple(readings), time.monotonic())
        self._consumption.move_to_end(meter_id)
        while len(self._consumption) > self._consumption_max:
            self._consumption.popitem(last=False)
        return readings

    # --- introspection ----------------------------------------------------

    def info(self) -> SnapshotInfo:
        snapshot = self._snapshot
        return SnapshotInfo(
            built_at=snapshot.built_at if snapshot else None,
            age_seconds=round(snapshot.age_seconds(), 3) if snapshot else None,
            ttl_seconds=self._ttl,
            is_stale=self.is_stale(),
            meter_count=len(snapshot.meters) if snapshot else 0,
            transformer_count=len(snapshot.transformers) if snapshot else 0,
            source=snapshot.source if snapshot else "none",
            build_duration_ms=snapshot.build_duration_ms if snapshot else None,
            refresh_count=self._refresh_count,
            last_error=self._last_error,
        )


# --- query layer -----------------------------------------------------------


@dataclass
class MeterQuery:
    """Filters applied to the snapshot. Every field is optional and ANDed together."""

    search: str | None = None
    make: str | None = None
    install_status: str | None = None
    phase_type: str | None = None
    install_type: str | None = None
    build: str | None = None
    dt_code: str | None = None
    hierarchy_code: str | None = None
    has_location: bool | None = None
    with_issues: bool | None = None
    sort: str = "meter_id"
    descending: bool = False


_SORT_KEYS: dict[str, Callable[[Meter], object]] = {
    "meter_id": lambda m: m.meter_id,
    "serial_no": lambda m: (m.serial_no is None, m.serial_no or ""),
    "make": lambda m: (m.make is None, m.make or ""),
    "install_status": lambda m: (m.install_status is None, str(m.install_status or "")),
    "dt_code": lambda m: (m.dt_code is None, m.dt_code or ""),
}
SORTABLE_FIELDS = tuple(_SORT_KEYS)


def query_meters(meters: Sequence[Meter], query: MeterQuery) -> list[Meter]:
    """Apply filters and ordering. Pure and synchronous, so it is trivially testable."""
    result = list(meters)

    if query.search:
        needle = _fold(query.search)
        result = [
            m
            for m in result
            if needle in _fold(m.meter_id) or (m.serial_no and needle in _fold(m.serial_no))
        ]
    result = _filter_enum(result, query.make, lambda m: m.make)
    result = _filter_enum(result, query.install_status, lambda m: _value(m.install_status))
    result = _filter_enum(result, query.phase_type, lambda m: _value(m.phase_type))
    result = _filter_enum(result, query.install_type, lambda m: _value(m.install_type))
    result = _filter_enum(result, query.build, lambda m: _value(m.build))
    result = _filter_enum(result, query.dt_code, lambda m: m.dt_code)

    if query.hierarchy_code:
        wanted = query.hierarchy_code.strip().lower()
        result = [
            m for m in result if any((ref.code or "").lower() == wanted for ref in m.hierarchy)
        ]
    if query.has_location is not None:
        result = [m for m in result if (m.location is not None) == query.has_location]
    if query.with_issues is not None:
        result = [m for m in result if bool(m.data_quality) == query.with_issues]

    key = _SORT_KEYS.get(query.sort, _SORT_KEYS["meter_id"])
    result.sort(key=key, reverse=query.descending)
    return result


def meters_near(
    meters: Iterable[Meter], latitude: float, longitude: float, radius_km: float
) -> list[tuple[Meter, float]]:
    """Meters within ``radius_km`` of a point, nearest first.

    Linear scan with a haversine distance. At 403 meters this is well under a millisecond
    and needs no spatial index; the threshold at which that stops being true is documented
    in README.md under *Scaling*.
    """
    found: list[tuple[Meter, float]] = []
    for meter in meters:
        if meter.location is None:
            continue
        distance = haversine_km(
            latitude, longitude, meter.location.latitude, meter.location.longitude
        )
        if distance <= radius_km:
            found.append((meter, round(distance, 4)))
    found.sort(key=lambda pair: pair[1])
    return found


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres.

    Haversine rather than a flat-earth approximation: the error of the latter grows with
    latitude, and correctness here costs nothing at this data volume.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


def collect_meters_under(tree: HierarchyNode, node_id: str) -> set[str]:
    """Codes of every hierarchy node at or beneath ``node_id`` (used for subtree filters)."""
    node = hierarchy_ops.find_node(tree, node_id)
    if node is None:
        return set()
    codes: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if current.code:
            codes.add(current.code)
        stack.extend(current.children)
    return codes


def _meter_counts_by_dt(meters: Iterable[Meter]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for meter in meters:
        code = meter.dt_code or meter.hierarchy_code(HierarchyLevel.DT)
        if code:
            counts[code] = counts.get(code, 0) + 1
    return counts


def _filter_enum(
    meters: list[Meter], wanted: str | None, getter: Callable[[Meter], str | None]
) -> list[Meter]:
    if not wanted:
        return meters
    needle = wanted.strip().lower()
    return [m for m in meters if (getter(m) or "").lower() == needle]


def _value(enum_member: object) -> str | None:
    return getattr(enum_member, "value", None)


def _fold(text: str) -> str:
    """Case- and accent-insensitive key for substring matching.

    Matching happens locally against the snapshot, so the portal's unescaped ``LIKE``
    semantics (where ``_`` and ``%`` are wildcards) never apply - a caller searching for
    ``%`` correctly gets nothing.
    """
    return unicodedata.normalize("NFKD", text).casefold().strip()


def _utcnow() -> datetime:
    return datetime.now(UTC)
