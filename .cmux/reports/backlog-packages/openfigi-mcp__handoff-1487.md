# openfigi-mcp — package `handoff:1487`, item 1487

Branch: `proposal/openfigi-enrich-auth`-lineage, commit `954da10`.
Package size: 1 item (1487). Slice: that one item.

---

## 1. Reading the item — what is actually still open

Item 1487's title and tags arrived **empty** in the lane brief ("(title
missing)", `tags:`, `file:`), so the item text itself carried nothing to
read. What it carries is the handoff from
`auto-etf-scraper-handoff-1487-09271640`, and that is specific:

> Publish the inbound credential contract for POST /enrich — which header
> openfigi-mcp's new gate validates and which auth-mcp value mints it. The
> handoff text proposes `Authorization: Bearer <BOND_DATA_SUPABASE_SERVICE_KEY
> from auth-mcp>`, which looks like the wrong credential class for an inbound
> caller; any future etf-scraper caller should be written once against the
> real contract rather than guessed.

So the previous lane (`openfigi-mcp__theme-auth.md`, commits `ff15272` and
`f6fc3c9`) closed the *gate*, and in doing so created a new, smaller defect
that it did not close: **the gate exists but its contract is unpublished.**

## 2. Grouping the symptoms into underlying defects

I checked the tree before believing the handoff, and found the suspicion in it
is half right and half wrong — which matters, because the wrong half is the
half a consuming repo would have coded against.

**The suspicion is right that "Bearer token" reads like the wrong class.** A
`Bearer` credential normally means an OAuth/JWT-ish token minted *for* the
caller. This is not that. `server.py:_require_write_key` compares the supplied
string, `secrets.compare_digest`, against `supabase_writer.get_service_key()`
— literally the Supabase service-role key, verbatim, as a shared secret. The
`Authorization: Bearer` prefix is a transport convention here, not a
credential class. A caller who inferred "token" and went looking for a token
minting endpoint would find none.

**The suspicion is wrong that the value is the wrong one.** It is the correct
value and deliberately so: it is the ONE credential in this service a caller
cannot mint for itself. I re-verified the reasoning rather than taking it on
trust — `token_utils.validate_token` recomputes a public checksum, so a
self-minted token passes it (that rejection is pinned by
`test_checksum_token_is_not_sufficient`). The service-key gate is the only
option that is real access control without inventing a second secret.

**So: one defect, stated precisely.** *The gate's contract is asserted in code
but published nowhere a caller can read.* Concretely, before this change:

- `GET /brian-manifest` said `Authorization: Bearer <key>` — the placeholder
  `<key>` names neither the header's companion requirement nor the auth-mcp
  value. A consumer cannot write one line of correct code against it.
- The 401 body said the same `<key>` placeholder, so a caller who *failed* the
  gate learned nothing about which credential to go and fetch.
- The resolution order the gate actually honours
  (`BOND_DATA_SUPABASE_SERVICE_KEY` → `BOND_DATA_SUPABASE_KEY` →
  `SUPABASE_KEY`, in `supabase_writer._ensure_config:115`) was documented
  only in a code comment inside the resolver.

That is why the etf-scraper lane arrived at a guess. The guess was not
careless — there was nothing better in the tree to read. **That is the defect,
and it is at the producer, so that is where I fixed it.**

## 3. The fix — commit `954da10`. addresses 1487

The contract is now named **once, in code**, and both surfaces publish it from
that single definition, so the gate, the manifest and the 401 cannot drift
apart:

- `server.py:ENRICH_KEY_AUTH_MCP_NAMES` — the auth-mcp key names in the order
  the resolver tries them, each annotated with why it is in the list.
- `server.py:ENRICH_KEY_WHERE` — the prose contract: which value, how to
  retrieve it, that it is sent verbatim, the fallback order, and the failure
  modes (401 vs 503).
- The **manifest** description now appends `ENRICH_KEY_WHERE`.
- The **401** detail now appends `ENRICH_KEY_WHERE`. This is the part that
  turns a dead end into a fix: a caller that cannot authenticate is exactly
  the caller that needs to be told which credential to fetch.

**It still never echoes the value.** The 401 gained the *name* of the key, not
the key. `test_401_body_does_not_echo_the_expected_key` (pre-existing) still
passes, and I added a test asserting the name IS present — so the fix cannot
be "corrected" later by someone stripping the detail back to a bare
"Authentication required".

**The contract is now falsifiable.** `test_published_key_names_match_the_resolver`
reads `supabase_writer._ensure_config`'s own source and asserts every name
`ENRICH_KEY_AUTH_MCP_NAMES` publishes is a name that function actually
resolves. I verified this test is not vacuous by renaming the first published
name in a throwaway copy of the tree: it fails. Without it, the next rename in
the resolver would leave the manifest confidently describing a key nobody has
— which is the same defect, one layer down.

**No new secret, no new auth-mcp key, no new endpoint.** The credential is the
one `supabase_writer` already resolved for the write path. I added no
`os.environ` read. Nothing moved in `server.py` except these three additions.

**No client-facing financial number is touched.** This change adds prose to
two strings and a constant. No price, yield, spread, duration, NAV, cash or
P&L is read, written or computed anywhere in it. No STOP condition was hit.

## 4. What I deliberately did NOT fix

- **Every other id in this package.** There are none — the package is item
  1487 only, and it arrived with no title and no tags. Nothing else was in
  scope to leave out.
- **The gate's design itself.** Whether an inbound caller should present the
  *Supabase service-role key* at all is a real question (see §5), but it is a
  decision, not a defect: the credential works, is the only unmintable one
  available, and changing it means minting and distributing a new secret.
  I published the contract as it exists rather than re-litigating it. A future
  lane that wants a per-caller key should change the gate *and* this constant
  together — the test will hold them in step.
- **`POST /enrich` still being a blocking synchronous request** (the open half
  of #1497, carried forward by the previous lane's report). Unchanged and out
  of slice.
- **`AUTH_MCP_URL` read from `os.environ`** at `supabase_writer.py:60`. Still
  a house-rule violation in spirit (it is a service URL), still pre-existing,
  still out of slice. The previous lane flagged it; I am not widening this
  change to carry it.

## 5. Needs a human

1. **The previous lane's deploy blocker is unchanged and still the live one.**
   `ff15272` is committed, not deployed; this change does not deploy itself
   either. Between landing and deploy the deployed service is still open.
   After deploy, any caller not sending the header gets 401 — and now that 401
   tells them what to send, which is strictly better than before but is not
   the same as the caller being updated.
2. **Confirm `BOND_DATA_SUPABASE_SERVICE_KEY` is actually set in auth-mcp.**
   The gate fails closed: if it is missing, `/enrich` returns **503**, not 401
   — so a caller debugging this will see a 503 that looks like a deploy
   problem when it is a missing key. I verified the *name* is the canonical
   one estate-wide (identical in `orca_mcp/tools/supabase_client.py:150`,
   `athena-html-v3/views_client.py:1885`), but I cannot read auth-mcp's
   contents from here, so "the name is right" is verified and "the value is
   present" is not.
3. **Judgement for review, not a blocker:** sending a service-role Supabase
   key as a bearer credential means the credential's blast radius is larger
   than this endpoint's authority — anyone holding it can write the database
   directly. It is the only unmintable credential the service has, so it is
   the right *available* answer; a dedicated `OPENFIGI_ENRICH_KEY` registered
   in auth-mcp would be the better one. I did not invent that here, because
   minting a new secret is a decision (and would need the gate to accept both
   during a cutover). Flagging it as a deliberate, documented position.

## 6. Tests run

| Command | Result |
|---|---|
| `python3 -m pytest test_enrich_auth.py -q` | **19 passed** (16 pre-existing/unchanged + 3 new contract tests) |
| `python3 test_coupon_parser.py` | **PASS 20/20** (untouched; run to prove nothing in the parser path moved) |
| Drift check on a throwaway copy (`/tmp/drifttest`, since removed) | renaming the first published key makes `test_published_key_names_match_the_resolver` **fail** — the test is not vacuous |

No test asserted the old placeholder-only wording, so nothing had to be
weakened. I added 3 tests; I edited no pre-existing test.

## 7. Handoffs

The consuming repos still need their one-line doc edits. **The etf-scraper one
is now answerable — the contract it was guessing at is committed above.** No
code change is needed in either repo: the previous lane verified 0 references
to this service in etf-scraper, and bond-branding's is a documented manual
step.

<!-- lane-result
FIXED: 1487
ALREADY_FIXED: none
DECISION: none
-->

<!-- lane-handoffs
item: 1487
repo: bond-branding
change: bond-branding/TODO.md:21 says "Then call `POST /enrich` on the openfigi-mcp with those ISINs". The endpoint requires a credential: send `Authorization: Bearer <value>` where <value> is the Supabase service-role key published by auth-mcp as BOND_DATA_SUPABASE_SERVICE_KEY (fetch: GET {AUTH_MCP_URL}/api/key/BOND_DATA_SUPABASE_SERVICE_KEY, send verbatim). Update the line to name that header and key; the same contract is now published at GET /brian-manifest under "Batch Enrichment".
-->

<!-- lane-handoffs
item: 1487
repo: etf-scraper
change: The contract this handoff asked for is now published — read GET /brian-manifest ("Batch Enrichment") rather than guessing. POST /enrich takes `Authorization: Bearer <BOND_DATA_SUPABASE_SERVICE_KEY>` (or `X-API-Key: <same>`), fetched from auth-mcp by that exact name; a wrong key gives 401 and an unresolvable key on the service gives 503. No etf-scraper code change is needed today (tools/openfigi_enricher.py and bond_data_mcp/normalizer.py call the OpenFIGI API and Supabase directly); this is so that any future scheduled POSTer is written once against the real contract.
-->
