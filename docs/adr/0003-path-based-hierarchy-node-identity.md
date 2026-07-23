# ADR-0003: Identify hierarchy nodes by full ancestor path

**Status:** Accepted · **Date:** 2026-07-23

## Context

The portal exposes a seven-level network hierarchy (Zone → Circle → Division → Subdivision →
Substation → Feeder → DT) but only ever shows one meter's path at a time. The tree has to be
reconstructed by folding all 403 paths together.

The natural implementation keys nodes by code. Measured across the full export (blanks
excluded), that turns out to be unsafe:

| Relationship | Codes with more than one parent |
|---|---|
| circle → zone | 0 of 6 |
| **division → circle** | **10 of 10** |
| **subdivision → division** | **14 of 14** |
| **substation → subdivision** | **18 of 18** |
| **feeder → substation** | **12 of 28** |
| dt → feeder | 0 of 40 |

`D-01` appears under `C-01`, `C-03` **and** `C-05`. Keying by code merges three unrelated
divisions into a single node and sums their meters — with no error raised, just a wrong
number that looks entirely reasonable.

The data cannot tell us *why*. Either codes are legitimately scoped to their parent (ward and
circuit numbering often is), or the portal's records are simply inconsistent.

## Decision

A node's identity is the `/`-joined chain of ancestor codes: `Z-01/C-01/D-01`. A rung with a
blank code contributes a `?` placeholder rather than collapsing two distinct paths into one.
A missing rung truncates the path rather than attaching deeper nodes to the wrong parent.

Path construction lives in exactly one function — `domain/hierarchy.py::meter_path_segments`
— used by both the tree builder and the subtree-filter endpoint, so the two can never
disagree about what a meter's path is.

## Consequences

**Positive.** Correct under the "scoped codes" reading and lossless under the "inconsistent
data" reading: it preserves a distinction we may not fully understand rather than destroying
it. Meter counts are trustworthy at every level: 10 division codes correctly expand to 30
distinct division nodes, and 28 feeder codes to 40 feeder nodes.

**Negative.** Node ids are longer and must be treated as opaque by clients. A caller who
knows only a bare code cannot address a node directly — they filter meters by
`hierarchy_code` instead. If a division genuinely *is* shared across circles, it appears as
several nodes.

**Mitigation.** `/data-quality` reports the ambiguity with counts, so consumers see the
decision explicitly rather than inheriting it invisibly.

## Alternatives considered

**Key by bare code.** Rejected: silently corrupts meter counts. Pinned against by
`test_merging_by_code_would_have_been_wrong`.

**Key by `(level, code)`.** Rejected: prevents cross-level collisions but not the actual
problem, which is same-level codes appearing under different parents.

**Pick one parent (e.g. the most frequent) and discard the rest.** Rejected: destroys real
data to manufacture a tidy tree, and the discarded meters would vanish from their true branch.
