"""Canonical domain and API models.

These are the shapes this service promises its callers, and they are deliberately *not*
the portal's shapes. Design rules applied throughout:

* **snake_case** everywhere. The portal mixes ``camelCase`` (export), ``PascalCase``
  (``classData``) and ``Title Case With Spaces`` (SSR hierarchy). Callers see one convention.
* **Real types.** The portal returns numbers as strings and dates as ``DD/MM/YYYY HH:MM``.
  Here they are ``float``/``int``/``datetime``.
* **Explicit nullability.** A field that can be missing upstream is ``| None``, never an
  empty string or ``"N/A"`` sentinel, so a client can distinguish "unknown" from "empty".
* **Enums where the domain is closed**, with a passthrough for unrecognised values so a
  new meter status upstream degrades to a warning instead of a 500.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class StrEnum(str, Enum):
    """String enum whose unknown values are tolerated by :meth:`parse`."""

    @classmethod
    def parse(cls, value: Any) -> StrEnum | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        for member in cls:
            if member.value.lower() == text.lower():
                return member
        return None


class InstallStatus(StrEnum):
    INSTALLED = "installed"
    FAULTY = "faulty"
    DECOMMISSIONED = "decommissioned"


class PhaseType(StrEnum):
    SINGLE = "single"
    THREE = "three"


class InstallType(StrEnum):
    WHOLE_CURRENT = "whole_current"
    CT_OPERATED = "ct_operated"


class MeterBuild(StrEnum):
    """Which generation of the portal's data model a meter belongs to.

    Not cosmetic: it determines the shape of the per-meter detail payload upstream
    (``legacy`` -> parameter rows, ``v2`` -> a double-encoded ``classData`` blob).
    Exposed because it explains data-quality differences a consumer will otherwise
    find inexplicable.
    """

    LEGACY = "legacy"
    V2 = "v2"


# --- Hierarchy -------------------------------------------------------------


class HierarchyLevel(StrEnum):
    ZONE = "zone"
    CIRCLE = "circle"
    DIVISION = "division"
    SUBDIVISION = "subdivision"
    SUBSTATION = "substation"
    FEEDER = "feeder"
    DT = "dt"


#: Root-to-leaf order. The portal's own detail page renders exactly this sequence.
HIERARCHY_ORDER: tuple[HierarchyLevel, ...] = (
    HierarchyLevel.ZONE,
    HierarchyLevel.CIRCLE,
    HierarchyLevel.DIVISION,
    HierarchyLevel.SUBDIVISION,
    HierarchyLevel.SUBSTATION,
    HierarchyLevel.FEEDER,
    HierarchyLevel.DT,
)


class HierarchyRef(BaseModel):
    """One rung of a meter's network path."""

    model_config = ConfigDict(frozen=True)

    level: HierarchyLevel
    code: str | None = Field(
        None, description="Upstream code, e.g. 'DT-001'. Null if blank upstream."
    )
    name: str | None = Field(None, description="Display name. Null if blank upstream.")

    @property
    def is_complete(self) -> bool:
        return bool(self.code and self.name)


class HierarchyNode(BaseModel):
    """A node in the reconstructed network tree."""

    id: str = Field(description="Stable path-based identity, e.g. 'Z-01/C-01/D-01'.")
    level: HierarchyLevel | None = Field(
        None, description="Null only for the synthetic 'network' root."
    )
    code: str | None
    name: str | None
    meter_count: int = Field(description="Meters at or below this node.")
    children: list[HierarchyNode] = Field(default_factory=list)


HierarchyNode.model_rebuild()


# --- Meters ----------------------------------------------------------------


class GeoPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    latitude: float
    longitude: float


class Meter(BaseModel):
    """A smart meter, normalised across both upstream builds."""

    meter_id: str = Field(description="Portal identifier, e.g. 'J100001'. Case-sensitive.")
    serial_no: str | None
    make: str | None
    phase_type: PhaseType | None
    install_status: InstallStatus | None
    install_type: InstallType | None
    build: MeterBuild | None
    dt_code: str | None
    hierarchy: list[HierarchyRef] = Field(
        default_factory=list, description="Root-to-leaf network path."
    )
    location: GeoPoint | None = None
    data_quality: list[str] = Field(
        default_factory=list,
        description="Machine-readable slugs for anomalies detected in this record.",
    )

    def hierarchy_code(self, level: HierarchyLevel) -> str | None:
        for ref in self.hierarchy:
            if ref.level == level:
                return ref.code
        return None


class MeterSummary(BaseModel):
    """List-view projection. Omits the full hierarchy path to keep pages small."""

    meter_id: str
    serial_no: str | None
    make: str | None
    phase_type: PhaseType | None
    install_status: InstallStatus | None
    dt_code: str | None
    location: GeoPoint | None = None


class MeterWithDistance(MeterSummary):
    distance_km: float = Field(description="Great-circle distance from the query point.")


# --- Transformers ----------------------------------------------------------


class Transformer(BaseModel):
    """A distribution transformer, from the portal's authoritative DT registry."""

    code: str
    name: str | None
    feeder_code: str | None
    capacity_kva: int | None
    meter_count: int = Field(0, description="Meters attached, per the current snapshot.")


# --- Consumption -----------------------------------------------------------


class MeterReading(BaseModel):
    """One raw interval reading. ``kwh``/``kvah`` are **cumulative register values**."""

    timestamp: datetime
    kwh: float | None
    kvah: float | None
    voltage_r: float | None


class ConsumptionInterval(BaseModel):
    """Energy actually consumed between two consecutive readings."""

    start: datetime
    end: datetime
    duration_minutes: float
    kwh: float | None = Field(
        None, description="Consumption in the interval (delta of register)."
    )
    kvah: float | None = None
    average_voltage_r: float | None = None
    estimated: bool = Field(
        False, description="True when the interval spans a gap in the upstream series."
    )


class ConsumptionSummary(BaseModel):
    total_kwh: float | None
    total_kvah: float | None
    average_power_factor: float | None = Field(
        None, description="total_kwh / total_kvah. Null when kVAh is unavailable or zero."
    )
    peak_interval_kwh: float | None = Field(
        None, description="Largest single interval, at the granularity returned."
    )
    min_voltage_r: float | None
    max_voltage_r: float | None
    interval_minutes: float | None = Field(
        None, description="Modal sampling cadence inferred from the data, not assumed."
    )
    reading_count: int
    gap_count: int = Field(0, description="Missing sampling slots detected in the window.")


class ConsumptionResponse(BaseModel):
    meter_id: str
    window_start: datetime | None
    window_end: datetime | None
    granularity: str
    summary: ConsumptionSummary
    intervals: list[ConsumptionInterval]
    readings: list[MeterReading] = Field(
        default_factory=list,
        description="Raw cumulative register readings. Included only when requested.",
    )


# --- Envelopes -------------------------------------------------------------


class PageMeta(BaseModel):
    page: int
    page_size: int
    total_items: int
    total_pages: int
    has_next: bool
    has_previous: bool


class Page(BaseModel, Generic[T]):
    """Envelope for every collection response.

    An envelope rather than a bare array: it leaves room to add fields (cursors, facet
    counts) without breaking clients, and a top-level JSON array is a long-standing
    XSS-adjacent footgun in browsers.
    """

    items: list[T]
    meta: PageMeta


class SnapshotInfo(BaseModel):
    """Provenance for the local index - lets a caller reason about staleness itself."""

    built_at: datetime | None
    age_seconds: float | None
    ttl_seconds: int
    is_stale: bool
    meter_count: int
    transformer_count: int
    source: str = Field(description="Which upstream path produced it: 'export' or 'search'.")
    build_duration_ms: float | None = None
    refresh_count: int = 0
    last_error: str | None = None


class DataQualityIssue(BaseModel):
    code: str
    level: str = Field(description="'warning' or 'error'.")
    message: str
    affected_count: int
    sample_meter_ids: list[str] = Field(default_factory=list)


class DataQualityReport(BaseModel):
    generated_at: datetime
    meter_count: int
    issue_count: int
    issues: list[DataQualityIssue]
