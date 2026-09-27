"""
Tests for the /enrich write gate (#1487).

The defect: POST /enrich is a public production write to bond_reference with
no authentication and wildcard CORS. The item's originally-proposed fix —
validate with token_utils.validate_token() — was rejected (#3074) because
that function only recomputes a public SHA256 checksum, so any caller can
mint a passing token. These tests pin the gate that replaced it: a shared
secret the caller cannot mint, compared against the service key this service
already uses for its Supabase writes.

Run: python3 -m pytest test_enrich_auth.py -q
"""

import os
import sys

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

# supabase_writer raises at import time when AUTH_MCP_URL is unset.
os.environ.setdefault("AUTH_MCP_URL", "http://auth-mcp.invalid")

import server  # noqa: E402
import token_utils  # noqa: E402

SERVICE_KEY = "service-role-key-abc123"


@pytest.fixture
def client(monkeypatch):
    """A TestClient whose Supabase credential is a known literal."""
    monkeypatch.setattr(server, "_expected_write_key", lambda: SERVICE_KEY)
    return TestClient(server.app, raise_server_exceptions=False)


def _stub_enrich_body(monkeypatch):
    """Make an authorised /enrich run do nothing real and succeed."""
    monkeypatch.setattr(server, "_get_openfigi_key", lambda: None)
    monkeypatch.setattr(server, "rate_params", lambda k: {"batch_size": 10, "requests_per_minute": 20})
    monkeypatch.setattr(server, "_select_unchecked_isins", lambda limit, include_recheck: [])


# ── The exposure itself ────────────────────────────────────────────────────

def test_enrich_rejects_unauthenticated(client, monkeypatch):
    """A bare POST with no credential must not reach the write path."""
    _stub_enrich_body(monkeypatch)
    r = client.post("/enrich", json={"isins": ["XS1982113463"]})
    assert r.status_code == 401, r.text


def test_enrich_rejects_wrong_key(client, monkeypatch):
    _stub_enrich_body(monkeypatch)
    r = client.post("/enrich", json={"isins": ["XS1982113463"]},
                    headers={"Authorization": "Bearer not-the-key"})
    assert r.status_code == 401, r.text


def test_unauthenticated_request_never_calls_openfigi(client, monkeypatch):
    """A rejected caller must not burn a single OpenFIGI request."""
    called = []
    monkeypatch.setattr(server, "fetch_batch", lambda *a, **k: called.append(a) or {})
    r = client.post("/enrich", json={"isins": ["XS1982113463"]})
    assert r.status_code == 401
    assert called == []


def test_enrich_accepts_bearer(client, monkeypatch):
    _stub_enrich_body(monkeypatch)
    r = client.post("/enrich", json={"isins": []},
                    headers={"Authorization": f"Bearer {SERVICE_KEY}"})
    assert r.status_code == 200, r.text


def test_enrich_accepts_x_api_key(client, monkeypatch):
    _stub_enrich_body(monkeypatch)
    r = client.post("/enrich", json={"isins": []}, headers={"X-API-Key": SERVICE_KEY})
    assert r.status_code == 200, r.text


def test_gate_fails_closed_when_key_unresolvable(monkeypatch):
    """No resolvable key means refuse, not allow."""
    def _boom():
        raise RuntimeError("auth-mcp unreachable")

    monkeypatch.setattr(server, "_expected_write_key", _boom)
    c = TestClient(server.app, raise_server_exceptions=False)
    r = c.post("/enrich", json={"isins": ["XS1982113463"]})
    assert r.status_code == 503, r.text


def test_401_body_does_not_echo_the_expected_key(client, monkeypatch):
    _stub_enrich_body(monkeypatch)
    r = client.post("/enrich", json={"isins": []}, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert SERVICE_KEY not in r.text


# ── The rejected fix must not be reintroduced ──────────────────────────────

def test_checksum_token_is_not_sufficient(client, monkeypatch):
    """
    #3074's correction, as a test: a token minted by the caller passes
    validate_token() and must still be rejected by the gate.
    """
    _stub_enrich_body(monkeypatch)
    minted = token_utils.generate_token()
    assert token_utils.validate_token(minted) is True  # it is a "valid" token…
    r = client.post("/enrich", json={"isins": []},
                    headers={"Authorization": f"Bearer {minted}"})
    assert r.status_code == 401, "a self-minted checksum token must not authorise a write"


def test_gate_does_not_use_validate_token():
    """Guard against a later edit reintroducing the checksum scheme."""
    import inspect
    src = inspect.getsource(server._require_write_key)
    assert "validate_token" not in src
    assert "compare_digest" in src


# ── CORS narrowing ─────────────────────────────────────────────────────────

def test_post_enrich_is_not_cors_allowed(client):
    """
    The preflight succeeeds (200) but POST is not on the allow-list, so a
    browser will not let a cross-origin page make the call.
    """
    r = client.options(
        "/enrich",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    allowed = {m.strip() for m in r.headers.get("access-control-allow-methods", "").split(",")}
    assert "POST" not in allowed, r.headers.get("access-control-allow-methods")

    # …and a cross-origin POST that skips the preflight still gets no usable
    # response body, because the browser withholds it without allow-origin.
    r2 = client.post("/enrich", json={"isins": []}, headers={"Origin": "https://evil.example"})
    assert r2.status_code == 401


def test_read_only_gets_still_pass_cors(client):
    r = client.get("/health", headers={"Origin": "https://athena.example"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "*"


# ── The gate helper in isolation ───────────────────────────────────────────

def _fake_request(headers):
    scope = {"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    return Request(scope)


def test_require_write_key_helper_accepts_and_rejects(monkeypatch):
    monkeypatch.setattr(server, "_expected_write_key", lambda: SERVICE_KEY)
    scope_ok = _fake_request({"authorization": f"Bearer {SERVICE_KEY}"})
    # happy path raises nothing
    assert server._require_write_key(scope_ok) is None

    with pytest.raises(HTTPException) as e:
        server._require_write_key(_fake_request({}))
    assert e.value.status_code == 401


# ── #1497: the per-ISIN N+1 is gone ────────────────────────────────────────

def test_build_hit_rows_does_not_call_get_single(monkeypatch):
    """
    The coupon-upgrade sample used to issue one get_single() per matched ISIN
    (up to 500 HTTP round trips a batch). One paged read of isin,coupon
    replaces it.
    """
    def _no_per_isin(*a, **k):
        raise AssertionError("get_single() must not be called per ISIN")

    monkeypatch.setattr(server, "get_single", _no_per_isin)
    monkeypatch.setattr(server, "_stored_coupons", lambda: {"XS1": 4.38})
    monkeypatch.setattr(server, "get_rows", lambda *a, **k: [])

    hits = {"XS1": {"openfigi_name": "ARAMCO 4 3/8 04/16/49", "figi": "BBG1"}}
    result_hits, upgrades, conv_err, checked, matched = server._build_hit_rows(["XS1"], hits, dry_run=False)

    assert checked == 1 and matched == 1 and conv_err == 0
    assert result_hits["XS1"]["coupon_bbg"] == 4.375
    assert result_hits["XS1"]["figi"] == "BBG1"
    assert len(upgrades) == 1 and upgrades[0]["stored"] == 4.38


def test_build_hit_rows_dry_run_reads_no_stored_coupons(monkeypatch):
    """A dry run must not touch the DB at all."""
    monkeypatch.setattr(server, "_stored_coupons",
                        lambda: (_ for _ in ()).throw(AssertionError("dry_run must not read the DB")))
    hits = {"XS1": {"openfigi_name": "ARAMCO 4 3/8 04/16/49"}}
    _, upgrades, _, _, _ = server._build_hit_rows(["XS1"], hits, dry_run=True)
    assert upgrades == []


def test_unparseable_name_is_counted_not_silently_dropped(monkeypatch):
    monkeypatch.setattr(server, "_stored_coupons", lambda: {})
    hits = {"XS1": {"openfigi_name": "NO COUPON HERE"}}
    _, upgrades, conv_err, _, matched = server._build_hit_rows(["XS1"], hits, dry_run=False)
    assert conv_err == 1 and matched == 1 and upgrades == []


def test_write_rows_keep_the_same_key_set(monkeypatch):
    """#1481 regression guard: hit and miss rows must not diverge in shape."""
    monkeypatch.setattr(server, "_stored_coupons", lambda: {})
    hits = {"XS1": {"openfigi_name": "T 2 7/8 05/15/32"}}
    rows, _, _, _, _ = server._build_hit_rows(["XS1"], hits, dry_run=False)
    assert set(rows["XS1"].keys()) == {"isin", "openfigi_checked_at", "coupon_bbg", *server._OPENFIGI_FIELDS}


# ── The published inbound contract must match the code it describes ────────
#
# #1487 left a consuming repo's lane guessing which credential to send: the
# manifest said `Bearer <key>` without saying which auth-mcp value mints it,
# and the etf-scraper handoff proposed a token of unspecified origin. These
# tests make the published contract falsifiable, so a rename in the resolver
# cannot silently leave the manifest and the 401 describing a key nobody has.

def test_published_key_names_match_the_resolver():
    """
    ENRICH_KEY_AUTH_MCP_NAMES is documentation; _ensure_config is the code.
    Assert the documented order against the real source so they cannot drift.
    """
    import inspect
    import supabase_writer
    src = inspect.getsource(supabase_writer._ensure_config)
    key_line = [ln for ln in src.splitlines() if "BOND_DATA_SUPABASE_SERVICE_KEY" in ln]
    assert key_line, "resolver no longer reads BOND_DATA_SUPABASE_SERVICE_KEY"
    # The first published name must be the first one the resolver tries.
    assert server.ENRICH_KEY_AUTH_MCP_NAMES[0] == "BOND_DATA_SUPABASE_SERVICE_KEY"
    for name in server.ENRICH_KEY_AUTH_MCP_NAMES:
        assert name in key_line[0], f"{name} documented but not resolved by _ensure_config"


def test_manifest_publishes_where_the_key_comes_from(client):
    body = client.get("/brian-manifest").json()
    cap = next(c for c in body["capabilities"] if c["name"] == "Batch Enrichment")
    assert "BOND_DATA_SUPABASE_SERVICE_KEY" in cap["description"], cap["description"]
    assert "auth-mcp" in cap["description"]


def test_401_names_the_contract_but_never_the_value(client, monkeypatch):
    _stub_enrich_body(monkeypatch)
    r = client.post("/enrich", json={"isins": []}, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert "BOND_DATA_SUPABASE_SERVICE_KEY" in r.text, "a rejected caller must learn which key to fetch"
    assert SERVICE_KEY not in r.text, "the contract must never leak the value"
