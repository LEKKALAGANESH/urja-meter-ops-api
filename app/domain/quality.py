"""Data-quality reporting over the current snapshot.

A wrapper around a legacy system has a choice when the upstream data is inconsistent: hide
it behind a clean-looking response, or expose it. Hiding it is how downstream teams end up
debugging *our* API when the fault was always upstream.

So every anomaly the normaliser and hierarchy builder detect is aggregated here and served
at ``GET /api/v1/data-quality``. The endpoint doubles as regression detection: if the
portal's data drifts, the issue counts move.

Severity:
  * ``error``   - the record is unusable or internally contradictory.
  * ``warning`` - the record is usable but incomplete or ambiguous.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from .hierarchy import RepairReport
from .models import DataQualityIssue, DataQualityReport, Meter

_MAX_SAMPLES = 5

#: Human explanations for the slugs attached during normalisation. Anything not listed
#: still surfaces, with a generic message - an unknown slug must never be swallowed.
_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "partial_record_no_hierarchy": (
        "warning",
        "Record came from the paginated search fallback, which carries no hierarchy "
        "or location.",
    ),
    "dt_code_mismatch": (
        "error",
        "The meter's dtCode disagrees with the DT code in its own hierarchy path.",
    ),
    "dt_name_conflicts_registry": (
        "warning",
        "The meter's DT name differs from the authoritative name in the DT registry; "
        "the registry value was used.",
    ),
    "geo_out_of_range": (
        "error",
        "Coordinates fell outside valid latitude/longitude bounds and were dropped.",
    ),
    "hierarchy_missing": ("error", "The record carried no hierarchy at all."),
    "detail_unreadable": ("error", "The detail payload was not an object."),
    "detail_shape_unrecognised": (
        "error",
        "The detail payload matched neither the legacy nor the v2 shape.",
    ),
    "class_data_unparseable": (
        "error",
        "The v2 classData field was not valid JSON.",
    ),
    "class_data_missing_installed_meter": (
        "error",
        "The v2 classData JSON had no installed_meter object.",
    ),
}

_PREFIX_DESCRIPTIONS: tuple[tuple[str, str, str], ...] = (
    ("hierarchy_no_code_", "warning", "A hierarchy level had a name but a blank code."),
    ("hierarchy_no_name_", "warning", "A hierarchy level had a code but a blank name."),
    ("hierarchy_empty_", "warning", "A hierarchy level was present but entirely blank."),
    ("hierarchy_missing_", "warning", "A hierarchy level was absent from the record."),
    ("repaired_name_", "warning", "A blank name was repaired from another record's value."),
    ("repaired_code_", "warning", "A blank code was repaired from another record's value."),
)


def build_report(
    meters: Sequence[Meter], repair: RepairReport | None = None
) -> DataQualityReport:
    """Aggregate per-meter anomaly slugs and repair statistics into one report."""
    counts: dict[str, int] = {}
    samples: dict[str, list[str]] = {}

    for meter in meters:
        for slug in meter.data_quality:
            counts[slug] = counts.get(slug, 0) + 1
            bucket = samples.setdefault(slug, [])
            if len(bucket) < _MAX_SAMPLES:
                bucket.append(meter.meter_id)

    issues = [
        DataQualityIssue(
            code=slug,
            level=level,
            message=message,
            affected_count=count,
            sample_meter_ids=samples.get(slug, []),
        )
        for slug, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        for level, message in [_describe(slug)]
    ]

    if repair is not None:
        issues.extend(_repair_issues(repair, len(meters)))

    return DataQualityReport(
        generated_at=datetime.now(UTC),
        meter_count=len(meters),
        issue_count=len(issues),
        issues=issues,
    )


def _repair_issues(repair: RepairReport, meter_count: int) -> list[DataQualityIssue]:
    issues: list[DataQualityIssue] = []

    for level, count in sorted(repair.ambiguous_parents.items()):
        issues.append(
            DataQualityIssue(
                code=f"ambiguous_parent_{level}",
                level="warning",
                message=(
                    f"{count} distinct '{level}' code(s) appear under more than one parent. "
                    "Hierarchy nodes are therefore identified by full ancestor path, not by "
                    "code alone - see PROTOCOL.md."
                ),
                affected_count=count,
            )
        )

    for code, names in sorted(repair.dt_name_conflicts.items()):
        issues.append(
            DataQualityIssue(
                code="dt_registry_name_conflict",
                level="warning",
                message=(
                    f"DT {code} is named inconsistently across records: "
                    f"{sorted(names)}. The DT registry value was used."
                ),
                affected_count=1,
            )
        )

    for key, count in sorted(repair.unresolved.items()):
        issues.append(
            DataQualityIssue(
                code=f"unrepairable_{key}",
                level="warning",
                message=(
                    f"{count} blank '{key}' value(s) could not be repaired because the "
                    "dataset offered more than one candidate."
                ),
                affected_count=count,
            )
        )

    if repair.filled_codes or repair.filled_names:
        issues.append(
            DataQualityIssue(
                code="blanks_repaired",
                level="warning",
                message=(
                    f"Repaired {repair.filled_codes} blank code(s) and "
                    f"{repair.filled_names} blank name(s) from unambiguous matches "
                    f"elsewhere in the dataset of {meter_count} meters."
                ),
                affected_count=repair.filled_codes + repair.filled_names,
            )
        )
    return issues


def _describe(slug: str) -> tuple[str, str]:
    if slug in _DESCRIPTIONS:
        return _DESCRIPTIONS[slug]
    for prefix, level, message in _PREFIX_DESCRIPTIONS:
        if slug.startswith(prefix):
            return level, f"{message} (level: {slug[len(prefix) :]})"
    return "warning", "Unclassified anomaly reported by the normaliser."
