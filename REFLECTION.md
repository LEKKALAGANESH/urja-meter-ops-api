# REFLECTION.md

Answers to the five reflection questions.

---

## What assumptions did you make?

The ones that would actually change the output if wrong:

**Timestamps are naive local time.** The portal sends `DD/MM/YYYY HH:MM` with no timezone
and no documentation. The utility is in Jaipur, so IST (+05:30) is the obvious reading — but
"obvious" isn't "stated". I kept the values naive rather than stamping UTC on them, because
guessing wrong would shift every reading by 5½ hours *silently*, and a silent 5½-hour error
in energy data is far worse than an explicitly unresolved one. It's recorded as UNKNOWN in
`PROTOCOL.md` and it's the first thing I'd ask the portal team.

**Day-first dates.** This one I didn't have to assume — the data proves it. Series run to
`30/06/2026`, and there is no month 30.

**Hierarchy codes are scoped to their parent, not globally unique.** `D-01` appears under
three different circles. Either the utility legitimately reuses codes within a parent (ward
and circuit numbering often does), or the portal's records are inconsistent. The data cannot
distinguish these. I chose path-based node identity because it's *correct* under the first
reading and *lossless* under the second — it preserves a distinction I might not understand
rather than destroying it. The alternative, keying by code, would have merged unrelated
branches and quietly corrupted every meter count above them.

**The DT registry wins on names.** `DT-007` is "Sanganer DT 7" in `/portal/dts` but "Old
Malviya Nagar Xfmr" on three meters. I treated the registry as authoritative and reported
the conflict rather than picking silently.

**Registers can roll over.** Never observed in this dataset — every series is monotonic —
but physical meters wrap at their digit limit and get replaced. A negative delta returns
`null` rather than a negative number that would poison any downstream sum.

**~5 minutes of staleness is acceptable** for meter attributes and hierarchy, which change
on the order of weeks. Consumption is always fetched live, and every response carries
`X-Snapshot-Age-Seconds` so a caller can enforce something stricter.

---

## Which part was the most difficult, and how did you get unstuck?

**The hierarchy**, by a wide margin — and specifically the moment I realised my first
version of it was wrong.

Building a tree from 403 root-to-leaf paths is easy. I wrote it keyed on node code, it ran,
it produced a plausible-looking seven-level tree, and I nearly moved on. The thing that
stopped me was a habit rather than an insight: before trusting a derived structure, check
that its invariants hold. So I asked whether each child code had exactly one parent — and
found that **every** division, subdivision and substation code had several. `D-01` sat under
`C-01`, `C-03` and `C-05`.

My tree had been silently merging three unrelated divisions into one node and summing their
meters. It looked completely fine. That's what made it dangerous: no error, no warning, just
a wrong number that a downstream team would have built a report on.

Getting unstuck was less about the fix than about accepting I couldn't resolve the
ambiguity. I spent a while trying to determine which interpretation was *true* — was this
legitimate scoping or bad data? — before recognising that the portal simply doesn't contain
that information, and that waiting for certainty was the wrong move. The productive
reframing was: *which choice is safe under both interpretations?* Path identity is, because
it never destroys information. So I implemented that, documented the ambiguity explicitly in
`PROTOCOL.md`, surfaced it through `/data-quality`, and wrote a test named
`test_merging_by_code_would_have_been_wrong` so nobody "simplifies" it back later.

The runner-up was the HMAC signing on `/portal/export`. That was hard to *find* rather than
hard to solve — it's invisible in normal network traffic because the UI only signs when you
click Export. I found it by reading the page's JS chunk rather than watching requests, which
also turned out to be the single highest-leverage move in the whole exercise: grepping the
client bundle for `fetch(` handed me the entire private API surface in one pass, including
endpoints the UI barely uses.

---

## If you had another day, what would you improve?

**Resolve the timezone.** One conversation, and it removes the largest correctness risk in
the service. Nothing else on this list matters as much.

**Split `SnapshotStore`.** It currently does two jobs — snapshot lifecycle (fetch, refresh,
degrade) and query execution (filter, sort, proximity). They change for different reasons
and should be separate classes. I noticed this while writing its tests, which is exactly
when you find out an abstraction is doing too much, and left it because splitting it late
would have churned the layer everything else depends on.

**Add one true end-to-end test.** *(Done — `tests/test_end_to_end.py`.)* I had tested the app
with a fake store and the portal client against a mocked transport, but nothing exercised the
whole stack in a single test with the portal mocked at the HTTP boundary — the seam where an
integration bug would hide. That suite now drives the real app through `TestClient` with only
the portal's HTTP responses faked, covering dependency wiring, lifespan ordering, session
recovery, cross-layer error mapping and the snapshot economics.

**Anomaly detection on consumption.** Zero-consumption runs on `Installed` meters, voltage
excursions outside statutory limits, flatlined registers. The data supports all of it, and
it's plainly the next thing an operations team would ask for — it turns the service from a
data pipe into something that answers questions.

**Incremental refresh.** Rebuilding all 403 records on a timer to catch a handful of changes
is fine now and structurally wrong later, since refresh cost scales with estate size while
the change rate doesn't. It's blocked on the portal exposing a delta or change-timestamp,
which it doesn't, so it needs a real change-detection design rather than a quick fix.

---

## What mistake did you make while solving this?

Several, but the one worth telling is this: **I built a deliberately polite client, then
wrote a test suite that hammered the portal's login endpoint.**

Early on I probed for rate limiting by firing 25 sequential requests at
`/portal/meters/search`. All 200s, no rate-limit headers. I concluded "no rate limiting" and
moved on — and wrote that down as a finding.

That conclusion was wrong, and wrong in a specific way: I'd tested *data* routes and
generalised to the *whole portal*. Auth endpoints are exactly where rate limits usually
live, and I hadn't looked there.

It surfaced when my live contract tests started failing with `429`. Each test had its own
fixture, so each one logged in fresh — roughly 18 logins in a few seconds. The portal
throttled me, and my own session module reported it as an unhelpful
`unexpected login status 429`.

The irony is that I'd already built single-flight session refresh precisely so that
concurrent expiries wouldn't stampede the login endpoint. I'd reasoned carefully about that
failure mode in the production path and then walked straight into it from the test suite.

Three things came out of fixing it, and the failure was worth more than the tests were:

1. A **measured** limit rather than a guess — 3 logins per window, 4th returns `429` with a
   non-standard `x-retry-after: 10` header, recovering in ~10 s.
2. The session module now honours that header with bounded retries and surfaces
   `PortalRateLimited` → `503` + `Retry-After`, instead of a confusing generic auth error.
3. The live tests share one session for the whole module — which is both politer and a more
   realistic simulation of how the service actually behaves.

The lesson I'd keep: **I generalised from a probe that didn't cover the thing I was
claiming.** "No rate limiting on data routes at ~3 req/s" was the honest finding; "no rate
limiting" was an overreach. `PROTOCOL.md` now states the narrow version and marks the rest
UNKNOWN.

Two smaller ones, both caught by tests I'd written for other reasons:

* The route `/hierarchy/nodes/{node_id:path}/meters` returned 404 for everything, because
  the greedy `:path` converter on the sibling route swallowed `/meters` into the node id.
  Registration order fixes it; there's now a comment saying why it must not be "tidied".
* My snapshot store would, during an upstream export failure, *replace* a complete stale
  snapshot with a degraded fresh one that had no hierarchy and no coordinates — a downgrade
  dressed up as a recovery. Now the degraded path is only taken on a cold start.

---

## If you were reviewing your own submission, what would you criticise?

**The API surface is wider than the brief required.** `/stats`, `/data-quality`,
`/system/session`, subtree meter listings — each is individually justifiable, but a reviewer
could fairly say I optimised for demonstrating range over shipping the smallest thing that
solves the problem. `/data-quality` I'd defend hard, because surfacing upstream mess is a
genuine engineering position. `/stats` is closer to a nice-to-have.

**I found a real duplication doing this review, and fixed it rather than just noting it.**
`api/v1/hierarchy.py` had its own `_path_of` that rebuilt the same `/`-joined code path as
`domain/hierarchy.py::build_tree`. Two implementations of one rule: change either and the
subtree filter silently disagrees with the tree it filters against, with no error. It's now
a single `meter_path()` in the domain layer, used by both, with tests pinning the agreement.
Worth flagging that this survived my first pass — it's exactly the kind of defect that
reads as harmless until it isn't.

**The live tests reach into private attributes** — `client._session`, `client._http`. It
works, but it couples the tests to internals and would break on a harmless refactor. The
client should expose a small seam for this instead.

**The hierarchy endpoint has no pagination.** `depth` bounds it in practice, but a
`depth=7` request serialises the entire tree in one response. Fine at 40 leaf paths;
not a pattern that survives growth.

**Some error branches in the portal client are unreached.** Specifically the interaction
of retry-exhaustion with mid-flight re-authentication — I test each separately but not
together, and that combination is precisely where a subtle loop or a swallowed error would
live.

**Coverage is 91%, but it's unevenly distributed.** The domain layer — where the hard
reasoning lives — is at 98–99%. `main.py` and parts of the portal client's error paths are
lower. The number looks better than the weakest parts of the codebase actually are.

**I'd challenge my own snapshot-versus-proxy decision in review.** It's the right call and I
stand behind it, but it's also the decision that let me build the more interesting service,
and I should be honest that those two facts point the same way. If the export endpoint
disappeared tomorrow, the fallback path works but produces a materially poorer API — and
I've tested that it degrades, not that it degrades *acceptably*.

**Finally, on the optional web client:** my instinct was to skip it — a well-tested API with
an honest protocol write-up beats a thinner API plus a demo UI, and I still believe that
ordering. I built the console at `/app` only after the API and its tests were solid, and kept
it deliberately small: vanilla ES modules, no build step, under a strict `default-src 'self'`
CSP, with every view designed for its empty/loading/error states and verified in a real
browser. It earns its place by making the estate queries the portal can't answer *visible* —
but it is the last thing I'd cut under time pressure, not the first thing I'd polish.
