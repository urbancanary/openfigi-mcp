# openfigi-mcp — package `theme:auth`, item 1487

Branch: `proposal/openfigi-enrich-auth` (2 commits)
Package size: 1 item (1487), which absorbed #3074 and the merged #1497 job.

---

## 1. Grouping the symptoms into underlying defects

Item 1487 arrives as one item carrying three filings that read like one
vulnerability but are **two distinct defects** plus one piece of already-done
work:

| Filing | What it actually is |
|---|---|
| #1487 (canonical) — "POST /enrich is an unauthenticated public write endpoint with wildcard CORS" | **Defect A.** No credential on the write path, and CORS advertises POST to the world. |
| #3074 — "the checksum token scheme is not access control" | **Not a separate defect.** It is a correction to #1487's *proposed fix*, and it is correct. See §2. |
| #1497 (merged job) — "long blocking request with no progress or job mode" | **Defect B**, with a concrete measurable core: an N+1 inside the batch loop. |

The merge note says the job is "unauthenticated wildcard-CORS write" (A) plus
"blocking sync request and vanished run stats" (B). #1495, the "vanished run
stats" half, was already fixed on `main` before this lane started
(`_log_event` → logfmt lines, `_last_run` summary on the status page). I did
**not** re-do it.

## 2. What the fix must not be

The item text proposes gating `/enrich` with `token_utils.validate_token()`.
Absorbed duplicate #3074 rejected that, correctly, and I re-verified the
rejection in this tree rather than taking it on trust:

`token_utils.py:63` recomputes `sha256(random_part)[:8]` and compares it to
the caller's own suffix. `generate_token()` (`token_utils.py:30`) mints
exactly that, from `secrets.token_hex(8)`, with no secret involved. So
`validate_token()` returns `True` for **any** token the caller just made up.
Gating on it adds ceremony and would have closed 1487 while leaving the public
write wide open.

`test_enrich_auth.py::test_checksum_token_is_not_sufficient` asserts the
caller-minted token still gets a 401. There is a second test,
`test_gate_does_not_use_validate_token`, that greps the gate's own source, so
a later lane cannot quietly reintroduce the checksum scheme.

## 3. Defect A — fixed: gate on a secret the caller cannot mint

Commit `ff15272`. **covers 1487, 3074.**

The one credential in this service a caller cannot mint is the Supabase
service key the write path already uses. It is already resolved from auth-mcp
by `supabase_writer._ensure_config()` — so the gate adds **no new secret and
no new auth-mcp key**. I exposed it as `supabase_writer.get_service_key()`
and compare with `secrets.compare_digest`.

- `/enrich` now takes `Authorization: Bearer <key>` or `X-API-Key: <key>`.
- **Order matters:** `_require_write_key(request)` is the first statement in
  the handler, before `_get_openfigi_key()` / `rate_params()` and long before
  the first batch. A rejected caller cannot burn one OpenFIGI request, let
  alone a rate budget. Pinned by `test_unauthenticated_request_never_calls_openfigi`.
- **Fails closed.** If no key is resolvable the write is refused (503), not
  allowed. An unreachable auth-mcp must not reopen the hole.
- Supplying nothing and supplying a wrong key get an **identical** 401 body,
  which does not echo the expected key (pinned by test).
- CORS narrowed to `allow_methods=["GET", "OPTIONS"]`. The browser surface
  here is the read-only status page and `/coupon-upgrades`; both still pass
  (pinned). Native callers — the enrichment runs, `/ops/probes`, curl — send
  no `Origin` and are unaffected by CORS either way, so this breaks nothing.
- `/brian-manifest` examples updated to show the header, and the endpoint
  summary says the key is required.

### ⚠️ Consequence a human must action — the method is committed but NOT deployed

`POST /enrich` is a breaking API change. Every caller must now send the
header, **and this branch is not deployed** (lane rule: no deploy). So between
this landing and the deploy, the deployed service is still open, and after the
deploy any un-updated caller will get a 401.

The caller I could find in the estate is `bond-branding/TODO.md:21`
("Then call `POST /enrich` on the openfigi-mcp with those ISINs") — a
documented human/manual step, not a scheduled job. That is a favourable
coincidence: I found **no automated producer of `/enrich` traffic** anywhere
under `/opt/work`. (The two other "OpenFIGI enrichers" in the estate —
`etf-scraper/tools/openfigi_enricher.py` and
`etf-scraper/tools/bond_data_mcp/normalizer.py` — call the OpenFIGI API and
Supabase directly, not this service.) See the two handoff blocks at the end
for the exact one-line doc edits.

## 4. Defect B — fixed: the N+1 in the batch loop

Commit `f6fc3c9`. **covers 1497.**

The `#1497` text attributes the 2.5+ minutes to OpenFIGI rate limiting, which
is true and is the API's own floor — not something this repo can fix. But the
loop had a second, self-inflicted cost the item only names in passing ("plus
N+1 `get_single` calls per matched ISIN for the coupon-upgrade sample"). That
one is ours:

- `server.py` called `get_single("bond_reference", isin)` **inside** the
  per-hit loop — up to 500 extra HTTP round trips on a full 500-ISIN run, on
  top of the OpenFIGI calls and the sleeps.
- Replaced with one paged read of the two small columns (`isin`, `coupon`),
  built once per batch via `_stored_coupons()`.
- Dry runs now touch the database zero times; they previously issued one
  `get_single` per hit even in dry-run mode. Pinned by
  `test_build_hit_rows_dry_run_reads_no_stored_coupons`.
- An unparseable Bloomberg name is counted as **`conversion_errors`** in the
  run stats and the `enrich_batch`/`enrich_summary` log lines instead of being
  silently `continue`d, so a parser regression is visible rather than showing
  up only as a smaller upgrade sample.

The row-building moved out of the request handler into a **pure function**,
`server._build_hit_rows()`, so the write path is testable without FastAPI.
This is a *refactor of where the code lives*, deliberately not a change to
what it does: same fields, same values, same uniform key set on every row
(#1481, pinned by `test_write_rows_keep_the_same_key_set`). I touched **no
financial calculation** — `coupon_bbg` still comes from
`parse_coupon_from_bbg_name` and the upgrade threshold is still
`coupon_precision_gain`; neither was edited.

## 5. What I deliberately did NOT fix

- **#1497's job-mode / progress half — left open on purpose.**
  `POST /enrich` is still one synchronous request that can block ~2.5 min.
  Fixing that properly means a `POST /enrich/jobs` → 202 + `GET /enrich/jobs/{id}`
  pair, which is a **new endpoint**, and the house rules are explicit: a new
  endpoint goes in `routes/<page>.py` as an `APIRouter`, and this repo has no
  `routes/` package yet. Creating one for a service with 7 endpoints is a
  structural change to the whole app (`server.py` would need `include_router`),
  not a small one — and it is exactly the change that would collide with every
  other lane touching this repo. I fixed the part of #1497 that is a
  correction (the N+1) and left the architectural half open rather than half-doing it.
- **#1487's "or bind the service to Railway's private network".** The item
  offers this as an alternative to auth. It is a **deployment-topology
  decision, not a code change** — nothing in this repo can express it, and it
  would break `/brian-manifest`'s public `base_url` and the status page. The
  bearer key achieves the same end without that cost, so I took the other branch.
- **`/coupon-upgrades` and `/` (status page) stay public.** They are read-only
  and are linked from the manifest as the engine's page. Narrowing CORS did
  not touch them. Flagging it as a known, deliberate position, not an oversight.
- **The `AUTH_MCP_URL` os.environ read** at `supabase_writer.py:60` predates
  this lane and is out of slice. It is a service URL, so it does belong behind
  auth-mcp eventually; noting it rather than widening this change.

## 6. Tests run

| Command | Result |
|---|---|
| `python3 -m pytest test_enrich_auth.py -q` | **16 passed** (12 auth/CORS, 4 for the N+1) |
| `python3 test_coupon_parser.py` | **PASS 20/20** (unchanged, run to prove the refactor didn't disturb the parser path) |

No test in this repo asserted the old unauthenticated behaviour, so nothing had
to be weakened to make the gate pass. `test_enrich_auth.py` is new and is the
only file I added.

## 7. Needs a human

1. **Deploy, then update callers.** The breaking change is committed, not
   deployed. Someone with deploy rights must land the branch *and* tell
   whoever runs the manual enrich that it now needs the header.
2. **Confirm the service key is actually set in auth-mcp** as
   `BOND_DATA_SUPABASE_SERVICE_KEY`. The gate fails closed, so if it is
   missing, `/enrich` will 503 rather than run. Given commit `ea89a66`
   ("write bond_reference with the service key", #5044) this should already be
   true, but a 503 is the failure mode if it is not.
3. Nothing in this change moves a client-facing financial number; no STOP
   condition was hit.

<!-- lane-result
FIXED: 1487, 3074, 1497
ALREADY_FIXED: none
DECISION: none
-->

<!-- lane-handoffs
item: 1487
repo: bond-branding
change: bond-branding/TODO.md:21 says "Then call `POST /enrich` on the openfigi-mcp with those ISINs". The endpoint now requires a credential, so the manual step needs the header, e.g. `curl -X POST -H "Authorization: Bearer $OPENFIGI_ENRICH_KEY" -H 'Content-Type: application/json' -d '{"isins": [...]}' https://openfigi-mcp-production.up.railway.app/enrich`. Update the line to name the header and where the key comes from (auth-mcp BOND_DATA_SUPABASE_SERVICE_KEY).
-->

<!-- lane-handoffs
item: 1487
repo: etf-scraper
change: No code change needed today — verified that tools/openfigi_enricher.py and bond_data_mcp/normalizer.py call the OpenFIGI API and Supabase directly, not this service's POST /enrich, so they are unaffected by the new credential gate. This handoff exists only so that if a future scheduled job ever starts POSTing to openfigi-mcp /enrich, it carries `Authorization: Bearer <BOND_DATA_SUPABASE_SERVICE_KEY from auth-mcp>` from the start rather than failing with a 401.
-->
