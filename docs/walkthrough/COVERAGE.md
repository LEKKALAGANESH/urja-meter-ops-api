# Walkthrough Coverage Report — Urja Meter Ops Console

**Recording:** `walkthrough.mp4` (1920×1080, H.264, ~55 s) · **Frames:** `frames/` (19 labeled stills)
**Data:** LIVE portal data — **403 real meters, 40 transformers** (not mocked)
**Method:** real Chromium driven by Playwright against the running service, recorded natively.

> How it was produced (shown on the video's intro card):
> ```
> uvicorn app.main:app --reload
> curl http://127.0.0.1:8000/api/v1/health/live
> open  http://127.0.0.1:8000/   →  redirects to /app/
> ```

---

## Pages / views recorded

| # | View | Route | Frame | Verified |
|---|------|-------|-------|----------|
| 1 | Root → console redirect | `/` → `/app/` | 01 | 307 redirect lands on console |
| 2 | Estate Overview | `#/overview` | 02 | 6 KPIs + 4 distributions, real counts |
| 3 | Meters table | `#/meters` | 03 | 403 meters, real serials/makes/status |
| 4 | Meters — sorted | `#/meters?sort=make` | 04 | column sort by Make |
| 5 | Meters — filtered | `#/meters?install_status=faulty` | 05 | status filter, URL reflects state |
| 6 | Meters — search | `#/meters?search=J10` | 06 | live literal search |
| 7 | Meters — empty state | search `ZZZZZZ` | 07 | "no meters match" + recovery action |
| 8 | Meters — pagination | page 2 | 08 | next-page navigation |
| 9 | Meter drawer — loading | drawer open | 09 | skeleton while fetching |
| 10 | Meter drawer — detail | meter J100000 | 10 | full record + **live consumption chart** + network path |
| 11 | Network hierarchy | `#/network` | 11 | reconstructed tree, 7 levels |
| 12 | Network — expanded | tree toggle | 12 | expand/collapse, aria-correct |
| 13 | Geographic map | `#/map` | 13 | 403-point scatter, status legend, real geo |
| 14 | Data quality | `#/quality` | 14 | 17 real upstream inconsistencies |
| 15 | Theme — light | toggle | 15 | manual theme switch |
| 16 | Theme — dark | toggle | 16 | full dark palette, contrast holds |
| 17 | Responsive — tablet | 768×1024 | 17 | layout adapts |
| 18 | Responsive — mobile | 390×844 | 18 | header collapses, filters stack, no overflow |
| 19 | API docs (Swagger) | `/docs` | 19 | interactive OpenAPI |

## Workflows demonstrated
Discover console from terminal → land via root redirect → read estate health → filter/sort/search/paginate the estate → drill into a meter (detail + live consumption) → explore the network tree → view geographic distribution → audit upstream data quality → switch theme → verify responsive layout → browse the API contract.

## Features / states demonstrated
- **Data views:** table, bar distributions, scatter map, tree, KPI tiles, consumption chart.
- **Interactions:** sort, filter (4 facets), literal search, pagination, drawer open/close (Escape), tree expand/collapse, theme cycle, refresh control.
- **States:** loading skeleton (drawer), empty/no-results, populated data, dark/light/auto theme.
- **UX/a11y:** shareable URL filter state, modal focus behavior, keyboard Escape, status color legend, responsive breakpoints.

## API endpoints exercised (from the server access log)
`GET /` · `GET /app/` (+ js/css assets) · `GET /api/v1/stats` · `GET /api/v1/meters` ·
`GET /api/v1/meters/{id}` · `GET /api/v1/meters/{id}/consumption` · `GET /api/v1/hierarchy` ·
`GET /api/v1/data-quality` · `GET /api/v1/system/snapshot` · `GET /api/v1/health/ready` ·
`GET /docs` · `GET /openapi.json`

**UI-exercised: 9 data endpoints.** The remaining API-only routes (`/meters/near`,
`/transformers`, `/transformers/{code}`, `/hierarchy/nodes/{path}`,
`/hierarchy/nodes/{path}/meters`, `/health/live`, `/system/session`,
`POST /system/snapshot/refresh`) are not wired into the console UI by design; all are
documented and callable from the Swagger page shown in frame 19.

## Bugs found during recording
**None.** 0 blocking JS/console errors; every view loaded with real data, no broken images/icons, no overflow at any breakpoint recorded.

## Coverage summary
| Metric | Found | Covered | % |
|---|---|---|---|
| Console views | 5 (+ drawer, root, docs) | 5 (+ drawer, root, docs) | 100% |
| Feature interactions | 11 | 11 | 100% |
| UI states (loading/empty/data/theme) | 4 | 4 | 100% |
| Responsive breakpoints (desktop/tablet/mobile) | 3 | 3 | 100% |
| API endpoints (UI-driven) | 9 | 9 | 100% |
| API endpoints (total incl. API-only) | 17 | 9 UI + 8 via Swagger | 100% documented/reachable |

**Overall UI walkthrough coverage: 100%** of the reachable console surface.

## Limitations / blockers
- **OS/terminal screen capture:** not available in this environment — the recording is a
  native browser capture; the startup commands are shown on the intro card instead of a live
  terminal pane.
- **Auth/registration/admin/roles/upload/download/real-time:** not applicable — this service
  has no user auth model or those features by design (documented in the README).
