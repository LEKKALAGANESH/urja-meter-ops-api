"""Reconstruction of the utility's network tree from per-meter hierarchy paths.

The portal never exposes the tree. It only ever tells you one meter's root-to-leaf path
(Zone -> Circle -> Division -> Subdivision -> Substation -> Feeder -> DT). The tree has to
be inferred by folding 403 such paths together, and the data fights back in three ways.

**1. Mid-level codes are not globally unique.** Measured across the full export: every
division, subdivision and substation code appears under more than one parent - ``D-01``
occurs under ``C-01``, ``C-03`` and ``C-05``. Only zone->circle and feeder->dt are clean.

So a node's identity is its **full ancestor path**, not its bare code. Keying on the code
alone would fold three genuinely distinct divisions into one and silently corrupt every
meter count above it. Measured on the repaired data, 10 division codes expand to 30
distinct division nodes; DT codes alone are 1:1 with their paths (40).

*Interpretation, stated honestly:* the data cannot tell us whether codes are legitimately
scoped to their parent (as ward or circuit numbering often is) or whether the portal's own
records are inconsistent. Path identity is correct under the first reading and lossless
under the second - it preserves the distinction instead of destroying it - so it is the
safer default either way. ``/data-quality`` reports the ambiguity explicitly.

**2. Blank codes and names.** 22 meters carry a hierarchy rung with an empty ``code`` or
empty ``name``. Where the dataset itself resolves the blank unambiguously we repair it (see
:func:`repair_blanks`); where it does not, the blank is preserved and flagged.

**3. Conflicting DT names.** ``DT-007`` is ``"Sanganer DT 7"`` for ten meters and
``"Old Malviya Nagar Xfmr"`` for three. The DT registry at ``/portal/dts`` is authoritative,
so registry names win and the conflict is reported.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .models import (
    HIERARCHY_ORDER,
    HierarchyLevel,
    HierarchyNode,
    HierarchyRef,
    Meter,
    Transformer,
)

logger = logging.getLogger(__name__)

#: Placeholder segment for a rung with no usable code, so a blank never collapses two
#: distinct paths into one.
UNKNOWN_SEGMENT = "?"


@dataclass
class RepairReport:
    """What :func:`repair_blanks` was and was not able to fix."""

    filled_names: int = 0
    filled_codes: int = 0
    unresolved: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    dt_name_conflicts: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    ambiguous_parents: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def repair_blanks(
    meters: Sequence[Meter], transformers: Sequence[Transformer] | None = None
) -> tuple[list[Meter], RepairReport]:
    """Fill blank hierarchy codes/names where the dataset resolves them unambiguously.

    Two lookup tables are built from the *complete* rungs across all meters: code -> names
    and name -> codes, per level. A blank is filled only when its counterpart maps to
    exactly one candidate. One-to-many mappings are left untouched and counted, because
    guessing there would fabricate structure that the data does not support.

    DT names are additionally reconciled against the authoritative registry.

    Returns:
        The repaired meters (new objects; inputs are not mutated) and a report of what
        changed, which feeds ``/data-quality``.
    """
    report = RepairReport()
    registry = {t.code: t for t in (transformers or []) if t.code}

    code_to_names: dict[tuple[HierarchyLevel, str], set[str]] = defaultdict(set)
    name_to_codes: dict[tuple[HierarchyLevel, str], set[str]] = defaultdict(set)
    for meter in meters:
        for ref in meter.hierarchy:
            if ref.code and ref.name:
                code_to_names[(ref.level, ref.code)].add(ref.name)
                name_to_codes[(ref.level, ref.name)].add(ref.code)

    # Registry names are authoritative for DTs, so seed them before repairing.
    for code, transformer in registry.items():
        if transformer.name:
            observed = code_to_names[(HierarchyLevel.DT, code)]
            if observed and observed != {transformer.name}:
                report.dt_name_conflicts[code] = observed | {transformer.name}
            code_to_names[(HierarchyLevel.DT, code)] = {transformer.name}

    repaired: list[Meter] = []
    for meter in meters:
        refs: list[HierarchyRef] = []
        quality = set(meter.data_quality)
        for ref in meter.hierarchy:
            code, name = ref.code, ref.name

            if code and ref.level == HierarchyLevel.DT and code in registry:
                authoritative = registry[code].name
                if authoritative and name and name != authoritative:
                    quality.add("dt_name_conflicts_registry")
                if authoritative:
                    name = authoritative

            if code and not name:
                candidates = code_to_names.get((ref.level, code), set())
                if len(candidates) == 1:
                    name = next(iter(candidates))
                    report.filled_names += 1
                    quality.add(f"repaired_name_{ref.level.value}")
                else:
                    report.unresolved[f"name_{ref.level.value}"] += 1
            elif name and not code:
                candidates = name_to_codes.get((ref.level, name), set())
                if len(candidates) == 1:
                    code = next(iter(candidates))
                    report.filled_codes += 1
                    quality.add(f"repaired_code_{ref.level.value}")
                else:
                    report.unresolved[f"code_{ref.level.value}"] += 1

            refs.append(HierarchyRef(level=ref.level, code=code, name=name))

        repaired.append(
            meter.model_copy(
                update={
                    "hierarchy": refs,
                    "data_quality": sorted(quality),
                }
            )
        )

    _count_ambiguous_parents(repaired, report)
    return repaired, report


def _count_ambiguous_parents(meters: Sequence[Meter], report: RepairReport) -> None:
    """Count codes that appear under more than one parent - the non-uniqueness evidence."""
    parents: dict[tuple[HierarchyLevel, str], set[str]] = defaultdict(set)
    for meter in meters:
        by_level = {ref.level: ref for ref in meter.hierarchy}
        for index in range(1, len(HIERARCHY_ORDER)):
            level, parent_level = HIERARCHY_ORDER[index], HIERARCHY_ORDER[index - 1]
            ref, parent = by_level.get(level), by_level.get(parent_level)
            if ref and ref.code and parent and parent.code:
                parents[(level, ref.code)].add(parent.code)
    for (level, _code), parent_codes in parents.items():
        if len(parent_codes) > 1:
            report.ambiguous_parents[level.value] += 1


def meter_path_segments(meter: Meter) -> list[str]:
    """The node-path segments for one meter, root-first.

    The single source of truth for how a meter maps onto tree node ids. Both
    :func:`build_tree` and the subtree-filter endpoint use it, so a change to path
    construction cannot desynchronise the tree from the queries that filter against it.

    A missing rung truncates the path: attaching deeper nodes to the wrong parent would be
    worse than stopping early.
    """
    segments: list[str] = []
    by_level = {ref.level: ref for ref in meter.hierarchy}
    for level in HIERARCHY_ORDER:
        ref = by_level.get(level)
        if ref is None:
            break
        segments.append(ref.code or UNKNOWN_SEGMENT)
    return segments


def meter_path(meter: Meter) -> str:
    """A meter's full node path, e.g. ``Z-01/C-01/D-01/...``."""
    return "/".join(meter_path_segments(meter))


def build_tree(meters: Iterable[Meter]) -> HierarchyNode:
    """Fold meter paths into a single tree rooted at a synthetic ``network`` node.

    Node identity is the ``/``-joined chain of ancestor codes, so ``Z-01/C-01/D-01`` and
    ``Z-01/C-03/D-01`` remain distinct nodes despite sharing the code ``D-01``.

    ``meter_count`` is cumulative: a meter increments every ancestor on its path, so a
    zone's count is the number of meters anywhere beneath it. Counting only direct
    children would make every non-leaf report zero, which is useless for the "how big is
    this feeder" question the endpoint exists to answer.
    """
    root = _MutableNode(node_id="network", level=None, code=None, name="Network")

    for meter in meters:
        cursor = root
        cursor.meter_count += 1
        segments: list[str] = []
        by_level = {ref.level: ref for ref in meter.hierarchy}
        for level in HIERARCHY_ORDER:
            ref = by_level.get(level)
            if ref is None:
                # A missing rung truncates the path: attaching deeper nodes to the wrong
                # parent would be worse than stopping here.
                break
            segments.append(ref.code or UNKNOWN_SEGMENT)
            node_id = "/".join(segments)
            child = cursor.children.get(node_id)
            if child is None:
                child = _MutableNode(
                    node_id=node_id, level=level, code=ref.code, name=ref.name
                )
                cursor.children[node_id] = child
            elif child.name is None and ref.name:
                child.name = ref.name
            child.meter_count += 1
            cursor = child

    return root.freeze()


def find_node(tree: HierarchyNode, node_id: str) -> HierarchyNode | None:
    """Locate a node by its path id. Iterative to stay safe on deep trees."""
    stack = [tree]
    while stack:
        node = stack.pop()
        if node.id == node_id:
            return node
        stack.extend(node.children)
    return None


def prune_depth(node: HierarchyNode, max_depth: int) -> HierarchyNode:
    """Return a copy truncated to ``max_depth`` levels below ``node``.

    Lets a caller fetch the top of a 7-level tree without transferring all 40 leaf paths.
    """
    if max_depth <= 0:
        return node.model_copy(update={"children": []})
    return node.model_copy(
        update={"children": [prune_depth(child, max_depth - 1) for child in node.children]}
    )


@dataclass
class _MutableNode:
    """Build-time scaffold. Kept private so the public surface stays immutable pydantic."""

    node_id: str
    level: HierarchyLevel | None
    code: str | None
    name: str | None
    meter_count: int = 0
    children: dict[str, _MutableNode] = field(default_factory=dict)

    def freeze(self) -> HierarchyNode:
        return HierarchyNode(
            id=self.node_id,
            level=self.level,
            code=self.code,
            name=self.name,
            meter_count=self.meter_count,
            children=[
                child.freeze()
                for child in sorted(
                    self.children.values(), key=lambda n: (n.code or "￿", n.name or "")
                )
            ],
        )
