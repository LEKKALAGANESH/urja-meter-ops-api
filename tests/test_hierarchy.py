"""Tests for hierarchy reconstruction - the messiest part of the dataset.

The regression these guard against is subtle and destructive: keying nodes by bare code
would merge unrelated branches and silently corrupt every meter count above them.
"""

from __future__ import annotations

from app.domain.hierarchy import (
    build_tree,
    find_node,
    meter_path,
    prune_depth,
    repair_blanks,
)
from app.domain.models import (
    HierarchyLevel,
    HierarchyRef,
    Meter,
    Transformer,
)


def make_meter(meter_id: str, **codes: tuple[str | None, str | None]) -> Meter:
    """Build a meter from ``level=(code, name)`` pairs, in canonical order."""
    order = [level for level in HierarchyLevel if level.value in codes]
    return Meter(
        meter_id=meter_id,
        serial_no=None,
        make=None,
        phase_type=None,
        install_status=None,
        install_type=None,
        build=None,
        dt_code=None,
        hierarchy=[
            HierarchyRef(level=level, code=codes[level.value][0], name=codes[level.value][1])
            for level in order
        ],
    )


class TestPathIdentity:
    def test_same_code_under_different_parents_stays_distinct(self):
        """The measured reality: D-01 occurs under C-01, C-03 and C-05."""
        meters = [
            make_meter(
                "M1", zone=("Z-01", "Z1"), circle=("C-01", "C1"), division=("D-01", "D1")
            ),
            make_meter(
                "M2", zone=("Z-01", "Z1"), circle=("C-03", "C3"), division=("D-01", "D1")
            ),
        ]
        tree = build_tree(meters)
        assert find_node(tree, "Z-01/C-01/D-01") is not None
        assert find_node(tree, "Z-01/C-03/D-01") is not None

    def test_merging_by_code_would_have_been_wrong(self):
        """Both D-01 nodes hold one meter each, not two in a single merged node."""
        meters = [
            make_meter(
                "M1", zone=("Z-01", "Z1"), circle=("C-01", "C1"), division=("D-01", "D1")
            ),
            make_meter(
                "M2", zone=("Z-01", "Z1"), circle=("C-03", "C3"), division=("D-01", "D1")
            ),
        ]
        tree = build_tree(meters)
        assert find_node(tree, "Z-01/C-01/D-01").meter_count == 1
        assert find_node(tree, "Z-01/C-03/D-01").meter_count == 1

    def test_identical_paths_do_merge(self):
        meters = [
            make_meter(f"M{i}", zone=("Z-01", "Z1"), circle=("C-01", "C1")) for i in range(3)
        ]
        tree = build_tree(meters)
        assert find_node(tree, "Z-01/C-01").meter_count == 3


class TestMeterCounts:
    def test_counts_are_cumulative_not_direct(self):
        """A zone must report every meter beneath it, not just its direct children."""
        meters = [
            make_meter("M1", zone=("Z-01", "Z1"), circle=("C-01", "C1")),
            make_meter("M2", zone=("Z-01", "Z1"), circle=("C-02", "C2")),
        ]
        tree = build_tree(meters)
        assert find_node(tree, "Z-01").meter_count == 2
        assert tree.meter_count == 2

    def test_root_total_equals_meter_count(self):
        meters = [make_meter(f"M{i}", zone=("Z-01", "Z1")) for i in range(7)]
        assert build_tree(meters).meter_count == 7

    def test_missing_rung_truncates_the_path(self):
        """Attaching a deeper node to the wrong parent is worse than stopping."""
        meter = make_meter("M1", zone=("Z-01", "Z1"), division=("D-01", "D1"))
        meter = meter.model_copy(
            update={
                "hierarchy": [meter.hierarchy[0]]  # zone only
            }
        )
        tree = build_tree([meter])
        assert find_node(tree, "Z-01") is not None
        assert find_node(tree, "Z-01/D-01") is None


class TestBlankRepair:
    def test_blank_code_filled_from_unambiguous_match(self):
        meters = [
            make_meter("M1", circle=("C-01", "Circle 1")),
            make_meter("M2", circle=(None, "Circle 1")),
        ]
        repaired, report = repair_blanks(meters)
        assert repaired[1].hierarchy[0].code == "C-01"
        assert report.filled_codes == 1

    def test_blank_name_filled_from_unambiguous_match(self):
        meters = [
            make_meter("M1", circle=("C-01", "Circle 1")),
            make_meter("M2", circle=("C-01", None)),
        ]
        repaired, report = repair_blanks(meters)
        assert repaired[1].hierarchy[0].name == "Circle 1"
        assert report.filled_names == 1

    def test_ambiguous_blank_is_left_alone_not_guessed(self):
        """Two candidates means the data does not resolve it - inventing one would be a lie."""
        meters = [
            make_meter("M1", circle=("C-01", "Shared")),
            make_meter("M2", circle=("C-02", "Shared")),
            make_meter("M3", circle=(None, "Shared")),
        ]
        repaired, report = repair_blanks(meters)
        assert repaired[2].hierarchy[0].code is None
        assert report.unresolved["code_circle"] == 1

    def test_repair_does_not_mutate_the_input(self):
        meters = [
            make_meter("M1", circle=("C-01", "Circle 1")),
            make_meter("M2", circle=("C-01", None)),
        ]
        repair_blanks(meters)
        assert meters[1].hierarchy[0].name is None

    def test_ambiguous_parents_are_counted_for_reporting(self):
        meters = [
            make_meter(
                "M1", zone=("Z-01", "Z1"), circle=("C-01", "C1"), division=("D-01", "D1")
            ),
            make_meter(
                "M2", zone=("Z-01", "Z1"), circle=("C-03", "C3"), division=("D-01", "D1")
            ),
        ]
        _, report = repair_blanks(meters)
        assert report.ambiguous_parents["division"] == 1


class TestRegistryIsAuthoritative:
    def test_registry_name_overrides_a_stale_alias(self):
        """DT-007 is 'Sanganer DT 7' in the registry and 'Old Malviya Nagar Xfmr' on 3 meters."""
        meters = [make_meter("M1", dt=("DT-007", "Old Malviya Nagar Xfmr"))]
        registry = [
            Transformer(
                code="DT-007", name="Sanganer DT 7", feeder_code="F-007", capacity_kva=63
            )
        ]
        repaired, report = repair_blanks(meters, registry)
        assert repaired[0].hierarchy[0].name == "Sanganer DT 7"
        assert "dt_name_conflicts_registry" in repaired[0].data_quality
        assert "DT-007" in report.dt_name_conflicts

    def test_agreeing_name_raises_no_conflict(self):
        meters = [make_meter("M1", dt=("DT-001", "Malviya Nagar DT 1"))]
        registry = [
            Transformer(
                code="DT-001", name="Malviya Nagar DT 1", feeder_code="F-001", capacity_kva=100
            )
        ]
        repaired, _ = repair_blanks(meters, registry)
        assert "dt_name_conflicts_registry" not in repaired[0].data_quality


class TestTreeShaping:
    def test_prune_depth_limits_levels(self):
        meters = [
            make_meter(
                "M1", zone=("Z-01", "Z1"), circle=("C-01", "C1"), division=("D-01", "D1")
            )
        ]
        tree = build_tree(meters)
        pruned = prune_depth(tree, 1)
        assert pruned.children[0].children == []

    def test_prune_depth_zero_returns_bare_node(self):
        tree = build_tree([make_meter("M1", zone=("Z-01", "Z1"))])
        assert prune_depth(tree, 0).children == []

    def test_find_node_returns_none_for_unknown_path(self):
        tree = build_tree([make_meter("M1", zone=("Z-01", "Z1"))])
        assert find_node(tree, "Z-99") is None

    def test_empty_estate_yields_an_empty_root(self):
        tree = build_tree([])
        assert tree.meter_count == 0 and tree.children == []


class TestPathConstructionIsShared:
    """The tree and the subtree filter must agree on what a meter's path is.

    They previously built it independently, so a change to one would silently produce
    wrong subtree results while the tree still looked correct.
    """

    def test_meter_path_matches_the_tree_node_id(self):
        meter = make_meter(
            "M1", zone=("Z-01", "Z1"), circle=("C-01", "C1"), division=("D-01", "D1")
        )
        tree = build_tree([meter])
        assert find_node(tree, meter_path(meter)) is not None

    def test_path_truncates_at_a_missing_rung(self):
        meter = make_meter("M1", zone=("Z-01", "Z1"))
        assert meter_path(meter) == "Z-01"

    def test_blank_code_becomes_a_placeholder_segment(self):
        """A blank must not collapse two distinct paths into one."""
        meter = make_meter("M1", zone=("Z-01", "Z1"), circle=(None, "C1"))
        assert meter_path(meter) == "Z-01/?"
