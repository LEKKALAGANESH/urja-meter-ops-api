"""Translation from the portal's messy payloads into canonical domain objects.

This module is where the legacy system's inconsistencies stop. Everything downstream -
the store, the API, the tests - works with clean, typed values only.

The four upstream representations reconciled here:

1. **Bulk export** - ``camelCase``, structured hierarchy, ``float`` coordinates.
   The richest source.
2. **Search rows** - ``camelCase`` subset, no hierarchy, no geo.
3. **SSR detail, ``build=legacy``** - rows of ``{parameterName, parameterValue}``
   with human labels like ``"Serial No"``.
4. **SSR detail, ``build=v2``** - ``{"classData": "<a JSON *string*>"}`` which parses to
   ``{"installed_meter": {"MeterId","SerialNo",...}}`` in ``PascalCase``.

Guiding principle: **never silently discard a value.** Anything that cannot be parsed is
recorded as a data-quality slug on the meter and surfaced through ``/data-quality``, so
upstream degradation is visible rather than quietly absorbed.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from .models import (
    HIERARCHY_ORDER,
    GeoPoint,
    HierarchyLevel,
    HierarchyRef,
    InstallStatus,
    InstallType,
    Meter,
    MeterBuild,
    MeterReading,
    PhaseType,
    Transformer,
)

logger = logging.getLogger(__name__)

#: The portal's interval timestamps, e.g. "24/06/2026 00:30". Day-first, no timezone.
PORTAL_TIMESTAMP_FORMAT = "%d/%m/%Y %H:%M"

#: Maps the SSR detail page's display labels onto canonical field names. The portal uses
#: "Sub Station" (with a space) at one level and "substation" at another; both land here.
_SSR_HIERARCHY_LABELS: dict[str, HierarchyLevel] = {
    "zone": HierarchyLevel.ZONE,
    "circle": HierarchyLevel.CIRCLE,
    "division": HierarchyLevel.DIVISION,
    "subdivision": HierarchyLevel.SUBDIVISION,
    "sub station": HierarchyLevel.SUBSTATION,
    "substation": HierarchyLevel.SUBSTATION,
    "feeder": HierarchyLevel.FEEDER,
    "dt": HierarchyLevel.DT,
}

#: Detail-payload keys -> canonical field, across both builds.
_DETAIL_FIELD_ALIASES: dict[str, str] = {
    "meter id": "meter_id",
    "meterid": "meter_id",
    "serial no": "serial_no",
    "serialno": "serial_no",
    "make": "make",
    "phase type": "phase_type",
    "phasetype": "phase_type",
    "installation status": "install_status",
    "installationstatus": "install_status",
    "installation type": "install_type",
    "installationtype": "install_type",
}

#: Matches the SSR hierarchy display format "Malviya Nagar DT 1 (DT-001)".
_NAME_WITH_CODE = re.compile(r"^(?P<name>.*?)\s*\((?P<code>[^()]+)\)\s*$")

_STATUS_ALIASES = {
    "installed": InstallStatus.INSTALLED,
    "faulty": InstallStatus.FAULTY,
    "decommissioned": InstallStatus.DECOMMISSIONED,
}
_PHASE_ALIASES = {
    "single": PhaseType.SINGLE,
    "three": PhaseType.THREE,
    "1": PhaseType.SINGLE,
    "3": PhaseType.THREE,
}
_INSTALL_TYPE_ALIASES = {
    "whole current": InstallType.WHOLE_CURRENT,
    "ct operated": InstallType.CT_OPERATED,
}
_BUILD_ALIASES = {"legacy": MeterBuild.LEGACY, "v2": MeterBuild.V2}


# --- scalar coercion -------------------------------------------------------


def clean_text(value: Any) -> str | None:
    """Collapse whitespace and map empty/placeholder values to ``None``.

    The portal uses ``""`` for missing hierarchy codes and names. Returning ``None``
    instead of ``""`` lets a consumer tell "unknown" apart from "deliberately blank".
    """
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text or text.lower() in {"n/a", "na", "null", "none", "-", "--"}:
        return None
    return text


def to_float(value: Any) -> float | None:
    """Parse a numeric value that upstream sends as a string.

    Tolerates thousands separators and stray unit suffixes; returns ``None`` rather than
    raising, because one unparseable reading must not fail an entire series.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = clean_text(value)
    if text is None:
        return None
    text = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:  # pragma: no cover - regex guarantees parseability
        return None


def to_int(value: Any) -> int | None:
    parsed = to_float(value)
    return int(parsed) if parsed is not None else None


def parse_timestamp(value: Any) -> datetime | None:
    """Parse the portal's ``DD/MM/YYYY HH:MM`` timestamps.

    Day-first is confirmed by observation: series run to ``30/06/2026``, and there is no
    month 30, so the first field cannot be a month. ISO-8601 is accepted as a fallback in
    case the portal ever normalises its own output.

    Timestamps carry no timezone. We keep them naive rather than stamping an arbitrary
    zone onto them - inventing UTC here would silently shift every reading by the utility's
    real offset (IST, +05:30). Documented in PROTOCOL.md as an open question.
    """
    text = clean_text(value)
    if text is None:
        return None
    try:
        return datetime.strptime(text, PORTAL_TIMESTAMP_FORMAT)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("unparseable timestamp", extra={"value": text})
        return None


def split_name_and_code(value: Any) -> tuple[str | None, str | None]:
    """Split ``"Feeder 2 (F-002)"`` into ``("Feeder 2", "F-002")``.

    Handles the degenerate forms seen live: ``"(F-003)"`` (name blank) and ``"Feeder 5"``
    (code absent).
    """
    text = clean_text(value)
    if text is None:
        return None, None
    match = _NAME_WITH_CODE.match(text)
    if not match:
        return text, None
    return clean_text(match.group("name")), clean_text(match.group("code"))


# --- meters ----------------------------------------------------------------


def meter_from_export(row: dict[str, Any]) -> Meter:
    """Build a :class:`Meter` from one bulk-export record - the richest upstream source."""
    quality: list[str] = []
    meter_id = clean_text(row.get("meterId"))
    if not meter_id:
        raise ValueError("export row has no meterId")

    hierarchy = _hierarchy_from_export(row.get("hierarchy"), quality)
    location = _geo_from_export(row.get("geo"), quality)

    dt_code = clean_text(row.get("dtCode"))
    dt_ref = next((r for r in hierarchy if r.level == HierarchyLevel.DT), None)
    if dt_code and dt_ref and dt_ref.code and dt_ref.code != dt_code:
        quality.append("dt_code_mismatch")

    return Meter(
        meter_id=meter_id,
        serial_no=clean_text(row.get("serialNo")),
        make=clean_text(row.get("make")),
        phase_type=_lookup(_PHASE_ALIASES, row.get("phaseType")),
        install_status=_lookup(_STATUS_ALIASES, row.get("installStatus")),
        install_type=_lookup(_INSTALL_TYPE_ALIASES, row.get("installType")),
        build=_lookup(_BUILD_ALIASES, row.get("build")),
        dt_code=dt_code or (dt_ref.code if dt_ref else None),
        hierarchy=hierarchy,
        location=location,
        data_quality=sorted(set(quality)),
    )


def meter_from_search_row(row: dict[str, Any]) -> Meter:
    """Build a :class:`Meter` from a search-listing row.

    Used only on the degraded path where the signed export is unavailable. Hierarchy and
    location are absent from this source; the record is tagged so consumers know the gap
    is upstream, not a bug here.
    """
    meter_id = clean_text(row.get("meterId"))
    if not meter_id:
        raise ValueError("search row has no meterId")
    return Meter(
        meter_id=meter_id,
        serial_no=clean_text(row.get("serialNo")),
        make=clean_text(row.get("make")),
        phase_type=_lookup(_PHASE_ALIASES, row.get("phaseType")),
        install_status=_lookup(_STATUS_ALIASES, row.get("installStatus")),
        install_type=None,
        build=None,
        dt_code=clean_text(row.get("dtCode")),
        hierarchy=[],
        location=None,
        data_quality=["partial_record_no_hierarchy"],
    )


def meter_from_ssr_detail(meter_id: str, node: dict[str, Any]) -> Meter:
    """Build a :class:`Meter` from the SSR detail page, handling **both** upstream builds.

    This is the fallback path for a single meter when the bulk export is unavailable. It
    is also the only place the two detail encodings meet, which is precisely why they are
    reconciled in one function rather than at the call sites.
    """
    quality: list[str] = []
    detail = node.get("detail")
    fields, build = _flatten_detail(detail, quality)

    hierarchy = _hierarchy_from_ssr(node.get("hierarchy"), quality)
    dt_ref = next((r for r in hierarchy if r.level == HierarchyLevel.DT), None)

    return Meter(
        meter_id=clean_text(fields.get("meter_id")) or meter_id,
        serial_no=clean_text(fields.get("serial_no")),
        make=clean_text(fields.get("make")),
        phase_type=_lookup(_PHASE_ALIASES, fields.get("phase_type")),
        install_status=_lookup(_STATUS_ALIASES, fields.get("install_status")),
        install_type=_lookup(_INSTALL_TYPE_ALIASES, fields.get("install_type")),
        build=build,
        dt_code=dt_ref.code if dt_ref else None,
        hierarchy=hierarchy,
        location=None,
        data_quality=sorted(set(quality)),
    )


def _flatten_detail(
    detail: Any, quality: list[str]
) -> tuple[dict[str, Any], MeterBuild | None]:
    """Reduce either detail encoding to one flat ``{canonical_field: value}`` dict."""
    if not isinstance(detail, dict):
        quality.append("detail_unreadable")
        return {}, None

    # build=v2: a JSON document embedded in a JSON string.
    if "classData" in detail:
        raw = detail.get("classData")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            quality.append("class_data_unparseable")
            return {}, MeterBuild.V2
        installed = (parsed or {}).get("installed_meter") if isinstance(parsed, dict) else None
        if not isinstance(installed, dict):
            quality.append("class_data_missing_installed_meter")
            return {}, MeterBuild.V2
        return _map_aliases(installed), MeterBuild.V2

    # build=legacy: a list of {parameterName, parameterValue} rows.
    rows = detail.get("data")
    if isinstance(rows, list):
        pairs = {
            row.get("parameterName"): row.get("parameterValue")
            for row in rows
            if isinstance(row, dict)
        }
        return _map_aliases(pairs), MeterBuild.LEGACY

    quality.append("detail_shape_unrecognised")
    return {}, None


def _map_aliases(source: dict[Any, Any]) -> dict[str, Any]:
    """Fold both upstream key conventions onto canonical field names.

    ``"Serial No"`` and ``"SerialNo"`` both normalise to ``serial_no`` by lower-casing and
    stripping spaces, so the two builds converge without a second lookup table.
    """
    mapped: dict[str, Any] = {}
    for key, value in source.items():
        if key is None:
            continue
        normalised = str(key).strip().lower()
        canonical = _DETAIL_FIELD_ALIASES.get(normalised) or _DETAIL_FIELD_ALIASES.get(
            normalised.replace(" ", "")
        )
        if canonical:
            mapped[canonical] = value
    return mapped


# --- hierarchy -------------------------------------------------------------


def _hierarchy_from_export(raw: Any, quality: list[str]) -> list[HierarchyRef]:
    """Read the export's structured ``{level: {name, code}}`` hierarchy."""
    if not isinstance(raw, dict):
        quality.append("hierarchy_missing")
        return []
    refs: list[HierarchyRef] = []
    for level in HIERARCHY_ORDER:
        node = raw.get(level.value)
        if not isinstance(node, dict):
            quality.append(f"hierarchy_missing_{level.value}")
            continue
        code = clean_text(node.get("code"))
        name = clean_text(node.get("name"))
        if code is None and name is None:
            quality.append(f"hierarchy_empty_{level.value}")
            continue
        if code is None:
            quality.append(f"hierarchy_no_code_{level.value}")
        if name is None:
            quality.append(f"hierarchy_no_name_{level.value}")
        refs.append(HierarchyRef(level=level, code=code, name=name))
    return refs


def _hierarchy_from_ssr(raw: Any, quality: list[str]) -> list[HierarchyRef]:
    """Read the SSR page's flat ``{"Zone": "Jaipur Zone 1 (Z-01)"}`` hierarchy."""
    if not isinstance(raw, dict):
        quality.append("hierarchy_missing")
        return []
    by_level: dict[HierarchyLevel, HierarchyRef] = {}
    for label, value in raw.items():
        level = _SSR_HIERARCHY_LABELS.get(str(label).strip().lower())
        if level is None:
            continue  # "Meter ID"/"Installation Status" also live in this dict.
        name, code = split_name_and_code(value)
        if name is None and code is None:
            quality.append(f"hierarchy_empty_{level.value}")
            continue
        if code is None:
            quality.append(f"hierarchy_no_code_{level.value}")
        if name is None:
            quality.append(f"hierarchy_no_name_{level.value}")
        by_level[level] = HierarchyRef(level=level, code=code, name=name)
    return [by_level[level] for level in HIERARCHY_ORDER if level in by_level]


def _geo_from_export(raw: Any, quality: list[str]) -> GeoPoint | None:
    if not isinstance(raw, dict):
        return None
    lat = to_float(raw.get("lat", raw.get("latitude")))
    lng = to_float(raw.get("lng", raw.get("longitude")))
    return _build_point(lat, lng, quality)


def geo_from_endpoint(raw: Any) -> GeoPoint | None:
    """Parse ``/portal/meters/{id}/geo``, which returns coordinates as **strings**."""
    if not isinstance(raw, dict):
        return None
    return _build_point(to_float(raw.get("latitude")), to_float(raw.get("longitude")), [])


def _build_point(lat: float | None, lng: float | None, quality: list[str]) -> GeoPoint | None:
    if lat is None or lng is None:
        return None
    if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        quality.append("geo_out_of_range")
        return None
    return GeoPoint(latitude=lat, longitude=lng)


# --- transformers & readings ----------------------------------------------


def transformer_from_row(row: dict[str, Any]) -> Transformer | None:
    code = clean_text(row.get("code"))
    if not code:
        return None
    return Transformer(
        code=code,
        name=clean_text(row.get("name")),
        feeder_code=clean_text(row.get("feederCode")),
        capacity_kva=to_int(row.get("capacityKva")),
    )


def reading_from_row(row: dict[str, Any]) -> MeterReading | None:
    """Parse one interval reading. Returns ``None`` if the timestamp is unusable.

    A reading without a valid timestamp cannot be ordered or differenced, so it has no
    meaning in a time series - dropping it is correct, and the count is reported in the
    consumption summary rather than hidden.
    """
    timestamp = parse_timestamp(row.get("timestamp"))
    if timestamp is None:
        return None
    return MeterReading(
        timestamp=timestamp,
        kwh=to_float(row.get("kwh")),
        kvah=to_float(row.get("kvah")),
        voltage_r=to_float(row.get("voltR")),
    )


def _lookup(aliases: dict[str, Any], value: Any) -> Any:
    text = clean_text(value)
    if text is None:
        return None
    resolved = aliases.get(text.lower())
    if resolved is None:
        logger.warning("unrecognised enum value", extra={"value": text})
    return resolved
