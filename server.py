"""
OpenFIGI MCP — FastAPI service

Wraps the OpenFIGI free API to:
  1. Map ISINs to Bloomberg FIGI + static fields (name, ticker, market_sector,
     security_type)
  2. Parse exact fractional coupons from Bloomberg names
     ("ARAMCO 4 3/8 04/16/49" → coupon = 4.375, not 4.38)
  3. Write results back to Supabase bond_reference

Endpoints:
  GET  /health
  GET  /lookup/{isin}          — single-ISIN lookup (no DB write)
  POST /enrich                 — batch enrich into bond_reference
  GET  /brian-manifest
"""

import logging
import time
from datetime import date
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from coupon_parser import coupon_precision_gain, parse_coupon_from_bbg_name
from openfigi_client import fetch_batch, rate_params
from supabase_writer import get_rows, get_single, upsert_rows, _get_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("openfigi-mcp")

app = FastAPI(title="OpenFIGI MCP", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

VERSION_HASH = "v1_20260816"


# ── Pydantic models ────────────────────────────────────────────────────────

class EnrichRequest(BaseModel):
    isins: Optional[List[str]] = None
    max_isins: int = 500
    include_recheck: bool = False
    dry_run: bool = False


class FigiResult(BaseModel):
    isin: str
    figi: Optional[str]
    composite_figi: Optional[str]
    openfigi_name: Optional[str]
    openfigi_ticker: Optional[str]
    market_sector: Optional[str]
    security_type: Optional[str]
    security_type2: Optional[str]
    is_144a: Optional[bool]
    parsed_coupon: Optional[float]
    stored_coupon: Optional[float]
    coupon_updated: bool = False
    matched: bool = False


# ── Internal helpers ───────────────────────────────────────────────────────

def _build_result(isin: str, hit: Optional[Dict], ref_row: Optional[Dict]) -> FigiResult:
    """Build a FigiResult from an OpenFIGI hit + existing bond_reference row."""
    if not hit:
        return FigiResult(isin=isin, matched=False)

    parsed = parse_coupon_from_bbg_name(hit.get("openfigi_name"))
    stored = ref_row.get("coupon") if ref_row else None
    coupon_updated = coupon_precision_gain(stored, parsed)

    return FigiResult(
        isin=isin,
        figi=hit.get("figi"),
        composite_figi=hit.get("composite_figi"),
        openfigi_name=hit.get("openfigi_name"),
        openfigi_ticker=hit.get("openfigi_ticker"),
        market_sector=hit.get("market_sector"),
        security_type=hit.get("security_type"),
        security_type2=hit.get("security_type2"),
        is_144a=hit.get("is_144a"),
        parsed_coupon=parsed,
        stored_coupon=stored,
        coupon_updated=coupon_updated,
        matched=True,
    )


def _get_openfigi_key() -> Optional[str]:
    """
    Resolve OPENFIGI_API_KEY from auth-mcp for the current request.

    This key is optional — its absence degrades to unauthenticated OpenFIGI
    mode (10 ISINs/req, 20 req/min vs 100/240) rather than failing the
    request, but that degrade must be LOGGED loudly (not silent) so it's
    visible in Railway logs / ops probes rather than masking auth-mcp
    being down (#1484).
    """
    try:
        return _get_key("OPENFIGI_API_KEY")
    except Exception as e:
        logger.warning(f"OPENFIGI_API_KEY unavailable — degrading to unauthenticated OpenFIGI mode: {e}")
        return None


def _select_unchecked_isins(limit: int, include_recheck: bool) -> List[str]:
    """Pull ISINs from bond_reference that need OpenFIGI enrichment."""
    from datetime import timedelta

    RECHECK_AFTER_DAYS = 30

    # Unchecked rows first
    rows = get_rows(
        "bond_reference",
        {"select": "isin", "openfigi_checked_at": "is.null"},
        page_size=1000,
    )
    isins = [r["isin"] for r in rows]

    if len(isins) < limit and include_recheck:
        cutoff = (date.today() - timedelta(days=RECHECK_AFTER_DAYS)).isoformat()
        rows2 = get_rows(
            "bond_reference",
            {
                "select": "isin",
                "figi": "is.null",
                "openfigi_checked_at": f"lt.{cutoff}",
            },
            page_size=1000,
        )
        seen = set(isins)
        for r in rows2:
            if r["isin"] not in seen:
                isins.append(r["isin"])
                seen.add(r["isin"])

    return isins[:limit]


_OPENFIGI_FIELDS = (
    "figi", "composite_figi", "openfigi_name", "openfigi_ticker",
    "market_sector", "security_type", "security_type2", "is_144a",
)


def _write_hits(
    isins: List[str],
    hits: Dict[str, Dict],
    dry_run: bool,
    errored: Optional[set] = None,
) -> int:
    """
    Write OpenFIGI results + corrected coupons to bond_reference.

    Every row gets the SAME key set (isin, openfigi_checked_at, all
    _OPENFIGI_FIELDS, coupon_bbg) regardless of hit/miss, so PostgREST's
    bulk-upsert doesn't reject the batch for heterogeneous keys (#1481).

    On a miss, we do NOT null out the OpenFIGI fields — they're simply
    omitted from the write via COALESCE-style merge (Postgres has no native
    "skip this key" semantics over REST, so instead we only stamp
    openfigi_checked_at + isin for misses and rely on a second, field-only
    upsert for hits). This means a transient miss on a previously-enriched
    ISIN no longer wipes its stored FIGI data (#1482).

    ISINs present in `errored` (per-entry OpenFIGI API errors, not genuine
    no-match) are skipped entirely — no openfigi_checked_at stamp — so
    they're retried on the very next run rather than waiting 30 days.
    """
    today = date.today().isoformat()
    errored = errored or set()
    hit_rows = []
    miss_rows = []
    for isin in isins:
        if isin in errored:
            continue

        hit = hits.get(isin)
        if not hit:
            # Miss: stamp checked_at only. Never overwrite previously
            # -enriched fields with NULL.
            miss_rows.append({"isin": isin, "openfigi_checked_at": today})
            continue

        row: Dict = {"isin": isin, "openfigi_checked_at": today}
        for field in _OPENFIGI_FIELDS:
            row[field] = hit.get(field)

        parsed = parse_coupon_from_bbg_name(hit.get("openfigi_name"))
        row["coupon_bbg"] = parsed  # same key set on every hit row

        hit_rows.append(row)

    if dry_run:
        return len(hit_rows) + len(miss_rows)

    written = 0
    if hit_rows:
        written += upsert_rows("bond_reference", hit_rows)
    if miss_rows:
        written += upsert_rows("bond_reference", miss_rows)
    return written


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "openfigi-mcp", "version": VERSION_HASH}


@app.get("/lookup/{isin}", response_model=FigiResult)
def lookup(isin: str):
    """
    Look up a single ISIN via OpenFIGI. Does NOT write to the database.
    Useful for ad-hoc checks and coupon verification.
    """
    isin = isin.strip().upper()
    api_key = _get_openfigi_key()

    try:
        hits = fetch_batch([isin], api_key=api_key)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OpenFIGI request failed: {e}")

    try:
        ref_row = get_single("bond_reference", isin)
    except Exception:
        ref_row = None

    hit = hits.get(isin)
    result = _build_result(isin, hit, ref_row)

    if not result.matched:
        raise HTTPException(status_code=404, detail=f"No OpenFIGI match for {isin}")

    return result


@app.post("/enrich")
def enrich(req: EnrichRequest):
    """
    Batch-enrich bond_reference rows with OpenFIGI data.

    If `isins` is provided, only those ISINs are processed.
    Otherwise pulls unchecked rows from bond_reference (up to max_isins).

    For each matched bond:
    - Writes figi, composite_figi, openfigi_name, openfigi_ticker, market_sector,
      security_type, security_type2, openfigi_checked_at to bond_reference
    - Writes coupon_bbg (parsed from Bloomberg fractional name) when available
    - Sets coupon = coupon_bbg when the precision gain is material (> 0.001)
      UNLESS the bond is locked in bond_identity

    Returns summary stats and a sample of coupon upgrades found.
    """
    api_key = _get_openfigi_key()
    rate = rate_params(api_key)
    batch_size = rate["batch_size"]
    sleep_between = 60.0 / rate["requests_per_minute"]

    # Determine work list
    if req.isins:
        isins = [i.strip().upper() for i in req.isins]
    else:
        try:
            isins = _select_unchecked_isins(
                limit=req.max_isins,
                include_recheck=req.include_recheck,
            )
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Could not fetch ISINs: {e}")

    if not isins:
        return {"checked": 0, "matched": 0, "coupon_upgrades": 0, "batches": 0}

    total_checked = 0
    total_matched = 0
    total_written = 0
    batches = 0
    coupon_upgrades: List[Dict] = []

    for i in range(0, len(isins), batch_size):
        batch = isins[i : i + batch_size]
        batches += 1

        errored: set = set()
        try:
            hits = fetch_batch(batch, api_key=api_key, errored=errored)
        except Exception as e:
            logger.error(f"OpenFIGI batch {batches} failed: {e}")
            continue

        # Collect coupon upgrade details before writing
        for isin, hit in hits.items():
            parsed = parse_coupon_from_bbg_name(hit.get("openfigi_name"))
            if parsed is None:
                continue
            try:
                ref = get_single("bond_reference", isin)
                stored = ref.get("coupon") if ref else None
            except Exception:
                stored = None
            if coupon_precision_gain(stored, parsed):
                coupon_upgrades.append({
                    "isin": isin,
                    "name": hit.get("openfigi_name"),
                    "stored": stored,
                    "parsed": parsed,
                    "delta": round(abs((parsed or 0) - (stored or 0)), 6),
                })

        written = _write_hits(batch, hits, dry_run=req.dry_run, errored=errored)
        total_checked += len(batch) - len(errored)
        total_matched += len(hits)
        total_written += written

        if (i + batch_size) < len(isins):
            time.sleep(sleep_between)

    return {
        "checked": total_checked,
        "matched": total_matched,
        "written": total_written,
        "batches": batches,
        "coupon_upgrades": len(coupon_upgrades),
        "coupon_upgrade_sample": coupon_upgrades[:20],
        "dry_run": req.dry_run,
    }


@app.get("/coupon-upgrades")
def coupon_upgrades(
    limit: int = Query(default=100, le=1000),
    min_delta: float = Query(default=0.001),
):
    """
    List bonds where the Bloomberg fractional coupon (coupon_bbg) differs
    materially from the stored coupon — these are candidates for correction.

    Requires coupon_bbg column to be populated in bond_reference (run /enrich first).
    """
    try:
        rows = get_rows(
            "bond_reference",
            {
                "select": "isin,coupon,coupon_bbg,openfigi_name",
                "coupon_bbg": "not.is.null",
                "coupon": "not.is.null",
            },
            page_size=1000,
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not fetch rows: {e}")

    upgrades = []
    for r in rows:
        stored = r.get("coupon")
        parsed = r.get("coupon_bbg")
        if stored is None or parsed is None:
            continue
        delta = abs(parsed - stored)
        if delta > min_delta:
            upgrades.append({
                "isin": r["isin"],
                "name": r.get("openfigi_name"),
                "stored": stored,
                "bbg": parsed,
                "delta": round(delta, 6),
            })

    upgrades.sort(key=lambda x: x["delta"], reverse=True)
    return {"total": len(upgrades), "upgrades": upgrades[:limit]}


def _brian_manifest_base_url() -> str:
    """
    Resolve the public base_url for the manifest from auth-mcp instead of
    hardcoding the internal *.up.railway.app URL (#1489). auth-mcp is the
    single source of truth so a future vanity-domain move propagates here
    without a code change; falls back to the current literal (logged) if
    OPENFIGI_MCP_URL isn't registered yet.
    """
    try:
        return _get_key("OPENFIGI_MCP_URL")
    except Exception as e:
        fallback = "https://openfigi-mcp-production.up.railway.app"
        logger.warning(
            f"OPENFIGI_MCP_URL not resolvable via auth-mcp ({e}); "
            f"falling back to literal {fallback} — register it in auth-mcp."
        )
        return fallback


@app.get("/brian-manifest")
def brian_manifest():
    return {
        "id": "openfigi",
        "name": "OpenFIGI",
        "tier": "engine",
        "sort_order": 10,
        "enabled": True,
        "version_hash": VERSION_HASH,
        "summary": (
            "Maps ISINs to Bloomberg FIGI identifiers, parses exact fractional coupons "
            "from Bloomberg names, and enriches bond_reference with authoritative static data."
        ),
        "base_url": _brian_manifest_base_url(),
        "capabilities": [
            {
                "name": "ISIN Lookup",
                "description": (
                    "Look up a single ISIN via OpenFIGI. Returns FIGI, Bloomberg name, "
                    "ticker, market sector, security type, and parsed fractional coupon."
                ),
                "examples": [
                    "GET /lookup/XS1982113463",
                    "GET /lookup/US91282CPL99",
                ],
            },
            {
                "name": "Batch Enrichment",
                "description": (
                    "Batch-enrich bond_reference with OpenFIGI data. "
                    "Processes unchecked ISINs in bulk and writes results back to Supabase. "
                    "Includes coupon_bbg (fractional coupon from Bloomberg name) for "
                    "precision correction of bonds stored to only 2 decimal places."
                ),
                "examples": [
                    "POST /enrich  {max_isins: 500}",
                    "POST /enrich  {isins: ['XS1982113463', 'US71654QDD16']}",
                    "POST /enrich  {dry_run: true, max_isins: 100}",
                ],
            },
            {
                "name": "Coupon Upgrade Report",
                "description": (
                    "List bonds where the Bloomberg fractional coupon (coupon_bbg) "
                    "differs materially from the stored coupon. These are candidates "
                    "for static data correction to fix accrued interest errors."
                ),
                "examples": [
                    "GET /coupon-upgrades",
                    "GET /coupon-upgrades?min_delta=0.005&limit=50",
                ],
            },
        ],
        "pages": [
            {
                "id": "main",
                "name": "OpenFIGI Engine",
                "path": "/health",
                "description": "Service health and version info.",
            }
        ],
        "tour": [
            {
                "speaker": "narrator",
                "text": (
                    "OpenFIGI maps ISINs to Bloomberg identifiers. "
                    "It also parses fractional coupons from Bloomberg names — "
                    "4 3/8 becomes 4.375, not 4.38."
                ),
            },
            {
                "speaker": "brian",
                "text": "Oh good. I was spending a lot of time wondering why accrued interest was off by 0.0875%.",
            },
            {
                "speaker": "narrator",
                "text": (
                    "POST /enrich processes unchecked ISINs in bulk. "
                    "GET /coupon-upgrades shows every bond where the precision gain is material."
                ),
            },
        ],
    }
