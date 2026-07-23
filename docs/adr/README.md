# Architecture Decision Records

One record per decision that was genuinely contested — where a competent engineer could
reasonably have chosen otherwise, and where knowing *why* matters six months from now.

Routine choices (FastAPI, pydantic, pytest) are not recorded here; they are conventional and
their rationale is in the README's trade-off table.

| # | Decision | Status |
|---|---|---|
| [0001](0001-snapshot-instead-of-passthrough-proxy.md) | Maintain a local snapshot instead of proxying each request | Accepted |
| [0002](0002-derive-consumption-from-cumulative-registers.md) | Expose derived consumption, not raw registers | Accepted |
| [0003](0003-path-based-hierarchy-node-identity.md) | Identify hierarchy nodes by full ancestor path | Accepted |
| [0004](0004-surface-data-quality-instead-of-hiding-it.md) | Surface upstream data-quality issues via the API | Accepted |
