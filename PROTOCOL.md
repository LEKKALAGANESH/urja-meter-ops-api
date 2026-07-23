# PROTOCOL.md — How the Urja Meter Ops portal actually works

Everything below was discovered by observing the running portal at
`https://urja-ops.flockenergy.tech` as a normal logged-in user. Read-only throughout: GETs,
plus the login the portal requires.

Each claim is tagged so you can tell what I proved from what I inferred:

| Tag | Meaning |
|---|---|
| **FACT** | Directly observed. Reproducible with the command shown. |
| **INFERENCE** | Reasoned from evidence, not observed directly. Stated with its reasoning. |
| **UNKNOWN** | Could not determine from a normal user's vantage point. |

The claims this service depends on are also encoded as executable tests in
[`tests/test_live_contract.py`](tests/test_live_contract.py) (`make test-live`), so this
document cannot silently rot.

---

## 1. How I approached it

The portal renders fine in a browser but has no API. My route in:

1. `GET /` → `302 /login`. The login page is server-rendered HTML with a plain
   `<form method="POST">` — no CSRF field, fields named `email` and `password`.
2. The response body carried `x-sveltekit-page: true` and a `link:` header preloading
   `_app/immutable/...` modules. **It is a SvelteKit app.** That single observation drove
   everything else, because it told me where to look next.
3. `GET /meters` after logging in returned HTML whose table body was **empty**, with a
   hydration payload ending `data: [null, {...user...}, null]`. A `null` node means that
   page's data is fetched **client-side** — so there had to be an internal JSON API.
4. I fetched the page's JS chunks and grepped them for `fetch(`. That yielded the entire
   private endpoint surface in one pass, including two endpoints the UI barely uses.

Reading the client bundle rather than clicking around is what made this fast. It also
turned up the HMAC signing scheme, which I would not have guessed from network traffic
alone.

```bash
# The step that unlocked everything: pull the page's JS chunk and read its fetch calls.
curl -s https://urja-ops.flockenergy.tech/_app/immutable/nodes/5.C4dYcCRt.js \
  | grep -o 'fetch(`[^`]*`'
# -> fetch(`/portal/meters/search?q=${...}&page=${...}`)
```

> Note: the hashed asset filenames (`5.C4dYcCRt.js`) change on every portal deploy. Find
> the current ones in the `link:` response header of any page.

---

## 2. Authentication

> The commands below read credentials from `$PORTAL_USERNAME` / `$PORTAL_PASSWORD`
> (`set -a; source .env`). This repository is public, so the credentials supplied with the
> assignment are deliberately not written into it — they grant access to a live portal, and
> a public repo is the wrong place for them regardless of who issued them.

### 2.1 Two ways in — I chose the second

**FACT.** The login form posts to `/login` as a SvelteKit form action:

```bash
curl -si https://urja-ops.flockenergy.tech/login \
  -H 'origin: https://urja-ops.flockenergy.tech' \
  -d "email=$PORTAL_USERNAME&password=$PORTAL_PASSWORD"
# HTTP/2 200
# set-cookie: __Secure-better-auth.session_token=...; Max-Age=3600; Path=/; HttpOnly; Secure; SameSite=Lax
# {"type":"redirect","status":303,"location":"/meters"}
```

Note the response is `200` with a *redirect described in the body* — a SvelteKit
convention, not an HTTP redirect.

**FACT.** The cookie name `__Secure-better-auth.session_token` identifies the
[better-auth](https://better-auth.com) library, which mounts its own JSON API at
`/api/auth/*`. That API exists and works:

```bash
curl -s https://urja-ops.flockenergy.tech/api/auth/sign-in/email \
  -H 'origin: https://urja-ops.flockenergy.tech' -H 'content-type: application/json' \
  -d "{\"email\":\"$PORTAL_USERNAME\",\"password\":\"$PORTAL_PASSWORD\"}"
# {"redirect":false,"token":"...","user":{...},"session":{"expiresAt":"2026-07-23T12:21:57.936Z",...}}
```

**This service uses `/api/auth/sign-in/email`, not the form action.** It returns the
session's `expiresAt` explicitly, which turns session management from guesswork into
arithmetic. The form action returns only an opaque redirect envelope.

`GET /api/auth/get-session` returns the current session or `{}` — used by the readiness
probe to confirm the cookie is genuinely still accepted upstream.

### 2.2 Session lifetime

**FACT.** `Max-Age=3600`, and `expiresAt` is consistently one hour after `createdAt`. A
long-running client *will* cross this boundary, so re-authentication is mandatory, not
optional.

**FACT.** `origin` header is required on auth POSTs. Without it better-auth replies
`403 {"code":"MISSING_OR_NULL_ORIGIN"}`.

### 2.3 Expiry is signalled **three different ways** — one of them lies

This is the single most important operational finding, and the easiest to get wrong.

| Route type | Expired-session response | Trap |
|---|---|---|
| `/portal/*` (JSON) | `401` + `{"error":"unauthorized"}` | none — honest |
| HTML pages (`/meters`) | `302` → `Location: /login` | must not follow redirects blindly |
| `/meters/{id}/__data.json` | **`200 OK`** + `{"type":"redirect","location":"/login"}` | **the status line claims success** |

```bash
# No cookie at all:
curl -s -o /dev/null -w '%{http_code}\n' https://urja-ops.flockenergy.tech/portal/dts?page=1   # 401
curl -s https://urja-ops.flockenergy.tech/meters/J100001/__data.json                            # 200 + redirect body
```

A client that only checks `response.status_code` will treat the third case as a successful
empty result and quietly serve wrong data. This service detects all three
(`app/portal/client.py::_is_session_rejected`).

### 2.4 Login is rate limited — data routes are not

**FACT.** I found this the hard way: my first live test run had each test log in
separately and started failing with `429`. Measured with a controlled burst:

```
attempt 1: 200    attempt 3: 200
attempt 2: 200    attempt 4: 429   x-retry-after: 10
                                   {"message":"Too many requests. Please try again later."}
```

* Threshold: the **4th** sign-in within the window.
* Recovery: ~10 s (a retry after 65 s succeeded).
* The hint header is **`x-retry-after`**, *not* the standard `Retry-After`.

**FACT.** Data routes showed no limit: 25 sequential `GET /portal/meters/search` calls all
returned `200` in 8.8 s (~2.8 req/s), with no rate-limit headers.

**INFERENCE.** The limit is auth-specific (better-auth ships this by default). I did not
probe data routes to destruction, so a higher limit may exist there. The client caps
concurrency at 6 regardless — absence of evidence isn't evidence of absence.

*Design consequence:* re-authentication is **single-flight**. Ten concurrent requests
hitting expiry must produce one login, not ten. Without that lock this limit turns a
routine expiry into a lockout.

---

## 3. The internal API

All routes require the session cookie. All return JSON. Discovered by reading the client
bundles, then verified individually.

| Endpoint | Returns | Notes |
|---|---|---|
| `GET /portal/meters/search?q=&page=` | `{data:[...], total:403}` | 20/page |
| `GET /portal/dts?page=` | `{data:[...], total:40}` | 20/page |
| `GET /portal/meters/{id}/energy` | `{data:[...]}` | fixed ~7-day window |
| `GET /portal/meters/{id}/geo` | `{data:{latitude,longitude}}` | **strings** |
| `GET /portal/keys` | `{data:{signingSecret}}` | for the export below |
| `GET /portal/export?page=1` | `{data:[...403], total:403}` | **HMAC-signed** |
| `GET /meters/{id}/__data.json` | SvelteKit devalue payload | SSR detail |

**FACT.** There is no per-meter detail JSON endpoint. `/portal/meters/{id}`,
`/portal/meters`, `/portal/hierarchy` all return the SPA's 404 HTML page. Detail is only
available through the SSR data endpoint.

### 3.1 The bulk export — the find that shaped the architecture

`/portal/export` is reachable from the Transformers page ("Export" button) and is the only
signed endpoint. **It returns all 403 meters in a single request**, and its records are
*richer* than anything else the portal exposes:

```jsonc
{
  "meterId": "J100000", "serialNo": "SE33962", "make": "HPL",
  "phaseType": "single", "installStatus": "Decommissioned",
  "installType": "Whole Current",
  "build": "legacy",                          // not exposed anywhere else
  "dtCode": "DT-001",
  "hierarchy": {                              // full structured path — not in search rows
    "zone":   {"name": "Jaipur Zone 1", "code": "Z-01"},
    "circle": {"name": "Circle 1",      "code": "C-01"},
    /* division, subdivision, substation, feeder, dt */
  },
  "geo": {"lat": 26.938961002479868, "lng": 75.83095696146852}   // floats, not strings
}
```

**FACT.** The `page` parameter is ignored — `page=1`, `page=2`, `page=99` and omitting it
entirely all return the complete set of 403. It must still be included in the signed
message if it is in the URL, because the signature covers the exact query string.

This is why the service maintains a local snapshot instead of proxying: one request buys
the entire estate with hierarchy and coordinates, which no other path provides.

### 3.2 The HMAC signing scheme

Reverse-engineered from `_app/immutable/nodes/4.*.js`:

```
message   = [METHOD, PATH, QUERY, TIMESTAMP].join("\n")
signature = hex(HMAC_SHA256(signingSecret, message))
headers   = { "x-timestamp": TIMESTAMP, "x-signature": signature }
```

`TIMESTAMP` is whole Unix seconds as a decimal string. Measured properties:

| Property | Result |
|---|---|
| Clock-skew tolerance | **±300 s inclusive**; ±301 s → `401 signature_invalid` |
| Bound to query string | signature for `page=1` rejected on `page=2` |
| Hex case | insensitive (uppercase accepted) |
| Replaces the session cookie? | **No** — valid signature without cookie → `401 unauthorized` |

```bash
# Reproduce (requires an authenticated cookie jar):
SECRET=$(curl -s -b jar https://urja-ops.flockenergy.tech/portal/keys | jq -r .data.signingSecret)
TS=$(date +%s)
SIG=$(printf 'GET\n/portal/export\npage=1\n%s' "$TS" | openssl dgst -sha256 -hmac "$SECRET" -hex | awk '{print $2}')
curl -s -b jar -H "x-timestamp: $TS" -H "x-signature: $SIG" \
  'https://urja-ops.flockenergy.tech/portal/export?page=1' | jq '.total'
# 403
```

**Note on `/portal/keys`:** the signing secret is handed to any authenticated user, so the
signature is not a security boundary against a logged-in client — it reads as a replay/CSRF
guard or an integrity check. I treat it purely as a protocol requirement to satisfy, and
the secret is fetched at runtime and never committed.

### 3.3 Search semantics — `q` is an unescaped SQL `LIKE`

**FACT.** `q` matches meter id and serial only, case-insensitively, as a substring. It does
**not** match make, status or DT code. Critically:

| Query | Total matches | |
|---|---|---|
| `J100001` | 1 | exact |
| `se33962` | 1 | case-insensitive |
| `HPL` / `DT-001` / `Installed` | 0 | other fields not searched |
| `%` | **403** | every meter |
| `_` | **403** | every meter |
| `J10000_` | 10 | single-char wildcard |
| `SE%` | 80 | prefix wildcard |
| `J100001 ` (trailing space) | 0 | not trimmed |

**INFERENCE.** `q` is interpolated into a `LIKE` pattern without escaping. `%` and `_`
behaving exactly as SQL wildcards is strong evidence. I did **not** attempt injection
beyond these two wildcard characters — the brief says treat the portal as read-only, and
establishing the wildcard behaviour was enough to build correctly against it.

*Design consequence:* this service does **not** delegate search upstream. It filters the
local snapshot, so `%` and `_` are literal characters and a search means what the caller
typed.

### 3.4 Pagination is unvalidated

**FACT.** `page=0` returns the same rows as `page=1`; `page=abc` also returns page 1;
`page=999` returns `[]` while `total` still reports 403.

*Design consequence:* a naive crawler that walks `page=0,1,2,…` silently duplicates 20
rows. This service validates pagination at its own edge (`page >= 1`) and stops a crawl on
an empty page rather than trusting `total`.

---

## 4. Data model and its quirks

### 4.1 Meter detail has **two incompatible shapes**

**FACT.** Every meter carries `build`, which determines the shape of its detail payload.
238 meters are `legacy`, 165 are `v2`.

`build=legacy` — label/value rows, human-readable labels:
```json
{"detail": {"data": [
  {"parameterName": "Serial No", "parameterValue": "SE33962"},
  {"parameterName": "Installation Status", "parameterValue": "Decommissioned"}
]}}
```

`build=v2` — a JSON document **encoded as a string inside** the JSON, with PascalCase keys:
```json
{"detail": {"classData": "{\"installed_meter\":{\"SerialNo\":\"SE65293\",\"InstallationStatus\":\"Faulty\"}}"}}
```

Same information, three naming conventions across the portal (`Serial No`, `SerialNo`,
`serialNo`). Normalising both onto one canonical model is
`app/domain/normalize.py::_flatten_detail`; the test asserting they converge is
`test_both_shapes_produce_identical_field_sets`.

The SSR hierarchy is a *third* convention — a flat dict of display strings:
`{"Zone": "Jaipur Zone 1 (Z-01)", "Sub Station": "Substation 1 (SS-01)", ...}`. Note
`"Sub Station"` with a space here versus `"substation"` in the export.

### 4.2 `__data.json` decoding, and its second trap

SvelteKit serves each page's server-load data as devalue-encoded JSON, where **integers are
pointers into a flat array**, not values:

```json
{"type":"data","nodes":[null,{"type":"data","data":[{"meterId":1},"J100001"]}]}
```
Index 0 is the root `{"meterId": <ref 1>}`; index 1 holds `"J100001"`. Decoder:
`app/portal/devalue.py`.

**FACT.** An unknown meter returns **`HTTP 200`** with an error node embedded in the body:

```bash
curl -s -b jar https://urja-ops.flockenergy.tech/meters/NOPE/__data.json
# 200 {"type":"data","nodes":[...,{"type":"error","error":{"message":"Meter not found"},"status":404}]}
```

So on this endpoint the transport status is meaningless twice over — once for expired
sessions (§2.3) and once for missing records. Both are handled explicitly.

### 4.3 Energy readings are **cumulative registers**

**FACT.** `kwh` and `kvah` increase monotonically across every meter sampled. `J100001`
runs 42594.05 → 42732.85 over seven days.

```json
{"timestamp": "24/06/2026 00:00", "kwh": "42594.47", "kvah": "46002.02", "voltR": "230"}
```

These are odometer readings, not per-interval usage. **Consumption is the difference
between consecutive readings.** Summing the `kwh` column would report ~14,000,000 kWh for a
household — plausible-looking nonsense in a JSON response, which is why it is pinned by a
test rather than a comment.

**FACT.** All values are **strings**; timestamps are `DD/MM/YYYY HH:MM` with **no
timezone**.

Day-first is proven, not assumed: series run to `30/06/2026`, and there is no month 30.

**FACT — two different sampling cadences:**

| Meters | Points | Interval |
|---|---|---|
| Typical (e.g. `J100001`) | 337 | **30 minutes** |
| `J100400`–`J100402` | 8 | **daily (1440 min)** |

Any code that hardcodes 30 minutes mislabels the daily meters. This service infers the
cadence from the data (modal gap, so one missing block doesn't skew it).

**FACT.** `/energy` ignores every query parameter. `?from=…&to=…`, `?days=1`, `?limit=10`,
`?page=1`, `?start=…&end=…` all return the identical full window. Date filtering must
therefore happen client-side.

**UNKNOWN.** The timezone of the timestamps. The utility is in Jaipur, so IST (+05:30) is
the natural reading, but nothing in the portal states it. This service keeps timestamps
**naive** rather than stamping an arbitrary zone on them — inventing UTC would silently
shift every reading by 5½ hours. Flagged as an open question for whoever owns the portal.

### 4.4 Geo is typed inconsistently

**FACT.** Same coordinate, two representations:

| Source | Value | Type |
|---|---|---|
| `/portal/meters/J100000/geo` | `"26.938961002479868"` | string |
| `/portal/export` | `26.938961002479868` | float |

Values agree exactly; only the encoding differs. Both are normalised to `float`.

### 4.5 The network hierarchy — and why it is genuinely hard

Seven levels: **Zone → Circle → Division → Subdivision → Substation → Feeder → DT.** The
portal only ever shows *one meter's* path. The tree must be reconstructed by folding all
403 paths together — and three problems surface when you do.

#### (a) Mid-level codes are **not globally unique** — FACT

Measured across the full export, excluding blanks:

| Relationship | Codes with >1 parent |
|---|---|
| circle → zone | 0 of 6 |
| **division → circle** | **10 of 10** |
| **subdivision → division** | **14 of 14** |
| **substation → subdivision** | **18 of 18** |
| **feeder → substation** | **12 of 28** |
| dt → feeder | 0 of 40 |

`D-01` appears under `C-01`, `C-03` **and** `C-05`. So 40 DT codes expand to **54 distinct
root-to-leaf paths**.

*Design consequence:* a node's identity is its **full ancestor path** (`Z-01/C-01/D-01`),
not its bare code. Keying by code alone would merge three unrelated divisions into one and
corrupt every meter count above them. Verified live:

```bash
curl -s localhost:8000/api/v1/hierarchy?depth=3 | jq '[..|objects|select(.code=="D-01")|{id,meter_count}]'
# [{"id":"Z-01/C-01/D-01","meter_count":20},
#  {"id":"Z-02/C-05/D-01","meter_count":10},
#  {"id":"Z-03/C-03/D-01","meter_count":10}]
```

**INFERENCE, stated honestly:** I cannot tell from the data whether codes are legitimately
scoped to their parent (ward/circuit numbering often is) or whether the portal's records
are simply inconsistent. Path identity is *correct* under the first reading and *lossless*
under the second — it preserves the distinction rather than destroying it — so it's the
safer default either way. `GET /api/v1/data-quality` reports the ambiguity explicitly
rather than hiding the decision.

#### (b) Blank codes and names — FACT

22 meters have a hierarchy rung with an empty `code` or empty `name`:

```
J100011  circle: {"name": "Circle 6", "code": ""}     # name, no code
J100218  circle: {"name": "",         "code": "C-01"} # code, no name
J100162  feeder: {"name": "",         "code": "F-003"}
```

Distribution: 6 circle codes, 5 feeder codes, 3 substation codes; 5 circle names, 3 feeder
names.

*Handling:* where the rest of the dataset resolves a blank **unambiguously** (exactly one
candidate), it is repaired and flagged `repaired_code_*` / `repaired_name_*`. Where two or
more candidates exist, the blank is **left alone** and counted as unrepairable — guessing
would fabricate structure. All 22 turned out to be unambiguously repairable.

#### (c) Conflicting DT names — FACT

`DT-007` is `"Sanganer DT 7"` on 10 meters and `"Old Malviya Nagar Xfmr"` on 3
(`J100400`–`J100402` — the same three meters that sample daily; they look deliberately
planted). The DT registry at `/portal/dts` says `Sanganer DT 7`.

*Handling:* the registry is treated as authoritative, the registry name wins, and the
conflict is reported at `/data-quality` rather than silently smoothed over.

### 4.6 Dataset shape

| Quantity | Value |
|---|---|
| Meters | 403 (`J100000`–`J100402`) |
| Distribution transformers | 40 |
| Distinct hierarchy paths | 54 leaf paths over 40 DT codes |
| Makes | HPL 90, Genus 88, Secure 85, Allied 76, L&T 64 |
| Install status | Installed 238, Faulty 90, Decommissioned 75 |
| Build | legacy 238, v2 165 |
| Meters with coordinates | 403 (100%) |
| Meters with a data-quality flag | 22 |

Meter ids are **case-sensitive**: `j100000` → `404`.

Serial numbers can contain `&` (e.g. `L&84997`) — harmless here, but worth knowing before
anyone builds a CSV export.

---

## 5. What I could not determine

* **UNKNOWN — timezone** of the energy timestamps (§4.3).
* **UNKNOWN — data-route rate limits.** None observed at ~3 req/s; I did not probe for the
  ceiling.
* **UNKNOWN — write operations.** The portal is read-only for this user and I made no
  attempt to find mutating endpoints.
* **UNKNOWN — whether `total` is ever inconsistent with the pages served.** It was always
  correct in testing; the client defends against the case anyway.
* **UNKNOWN — why `build` exists.** It cleanly predicts the detail payload shape, which
  reads like a partial data migration, but that is speculation.

---

## 6. Reproducing this

```bash
# Everything in this document is re-verified by:
cp .env.example .env    # fill in credentials
make test-live
```

18 contract tests covering authentication, all endpoint shapes, the signing scheme and its
skew window, cumulative registers, both builds still existing, the `LIKE` wildcard quirk,
and the non-unique hierarchy codes.
