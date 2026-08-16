"""
Thin OpenFIGI HTTP client.

Adapted from etf-scraper/tools/openfigi_enricher.py — extracted here as a
standalone module so the FastAPI server and any CLI scripts share the same
request logic without importing from etf-scraper.

Rate limits (as of OpenFIGI v3):
  Unauthenticated:  25 req/min,  10 ISINs per request  (~250 ISINs/min)
  Authenticated:    25 req/6s,  100 ISINs per request  (~25 000 ISINs/min)

Get a free API key at https://www.openfigi.com/api and store it in auth-mcp
as OPENFIGI_API_KEY.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("openfigi-mcp.client")

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

_WITH_KEY = {"batch_size": 100, "requests_per_minute": 240}
_NO_KEY   = {"batch_size": 10,  "requests_per_minute": 20}

_MAX_ATTEMPTS = 4
_BASE_DELAY_S = 2.0
_MAX_DELAY_S = 30.0


def _backoff_delay(attempt: int, retry_after: Optional[float]) -> float:
    """Exponential backoff with jitter, honouring Retry-After when present."""
    if retry_after is not None:
        return max(retry_after, 0.0)
    delay = min(_BASE_DELAY_S * (2 ** attempt), _MAX_DELAY_S)
    return delay * (0.5 + random.random())  # jitter: 0.5x-1.5x


def fetch_batch(
    isins: List[str],
    api_key: Optional[str] = None,
    errored: Optional[set] = None,
) -> Dict[str, Dict]:
    """
    POST a batch of ISINs to OpenFIGI.

    Returns {isin: fields_dict} for hits only.
    Genuine misses (no match from OpenFIGI) are omitted — caller should
    stamp openfigi_checked_at but leave figi NULL.

    If `errored` is passed, any ISIN whose per-entry response was an
    OpenFIGI-level error (not a genuine no-match) is added to it, so the
    caller can skip stamping openfigi_checked_at for those and retry them
    on the next run instead of waiting out the 30-day recheck window (#1482).

    Retries up to _MAX_ATTEMPTS times with exponential backoff + jitter on
    429, 5xx, timeouts, and connection errors; honours the Retry-After
    header on 429 when present. Raises on a non-2xx response after the
    final attempt, or on the final connection/timeout error (#1493).
    """
    if not isins:
        return {}

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key

    payload = [{"idType": "ID_ISIN", "idValue": isin} for isin in isins]

    resp = None
    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_ATTEMPTS):
        last_exc = None
        try:
            resp = requests.post(OPENFIGI_URL, json=payload, headers=headers, timeout=30)
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            resp = None
        else:
            if resp.status_code == 200:
                break
            if resp.status_code == 429 or resp.status_code >= 500:
                pass  # retryable, fall through to backoff below
            else:
                break  # non-retryable 4xx — don't waste attempts

        if attempt == _MAX_ATTEMPTS - 1:
            break

        retry_after = None
        if resp is not None and resp.status_code == 429:
            ra = resp.headers.get("Retry-After")
            if ra is not None:
                try:
                    retry_after = float(ra)
                except ValueError:
                    retry_after = None

        delay = _backoff_delay(attempt, retry_after)
        reason = f"HTTP {resp.status_code}" if resp is not None else repr(last_exc)
        logger.warning(f"OpenFIGI request failed ({reason}) — attempt {attempt + 1}/{_MAX_ATTEMPTS}, retrying in {delay:.1f}s")
        time.sleep(delay)

    if last_exc is not None:
        logger.error(f"OpenFIGI request failed after {_MAX_ATTEMPTS} attempts: {last_exc}")
        raise last_exc

    if resp is None or resp.status_code != 200:
        status = resp.status_code if resp is not None else "N/A"
        body   = resp.text[:300]  if resp is not None else ""
        logger.error(f"OpenFIGI HTTP {status}: {body}")
        if resp is not None:
            resp.raise_for_status()
        raise RuntimeError("OpenFIGI request failed")

    results = resp.json()  # list aligned with payload
    out: Dict[str, Dict] = {}
    for isin, entry in zip(isins, results):
        if not isinstance(entry, dict):
            continue
        if "error" in entry:
            # Per-entry API error (e.g. rate-limited item, malformed idValue)
            # is NOT the same as "no OpenFIGI match" — surface it so the
            # caller can skip stamping openfigi_checked_at rather than
            # treating a transient error as a definitive miss (#1482).
            logger.warning(f"OpenFIGI entry error for {isin}: {entry.get('error')}")
            if errored is not None:
                errored.add(isin)
            continue
        data = entry.get("data") or []
        if not data:
            continue
        # Prefer composite-level row; else take first hit
        hit = next(
            (d for d in data if d.get("figi") == d.get("compositeFIGI")),
            data[0],
        )
        name = hit.get("name") or ""
        sec_type = hit.get("securityType") or ""
        out[isin] = {
            "figi":            hit.get("figi"),
            "composite_figi":  hit.get("compositeFIGI"),
            "openfigi_name":   name,
            "openfigi_ticker": hit.get("ticker"),
            "market_sector":   hit.get("marketSector"),
            "security_type":   sec_type,
            "security_type2":  hit.get("securityType2"),
            "is_144a":         derive_is_144a(sec_type, name),
        }
    return out


def derive_is_144a(security_type: str, name: str) -> bool:
    """
    Derive 144A status from OpenFIGI securityType and name fields.

    True  → 144A-only tranche (not available to non-QIBs outside the US)
    False → RegS or Global (available internationally)

    Rules:
    - securityType == "144A"                    → True  (explicit)
    - name contains " 144A" token               → True  (name-embedded)
    - anything else (Global, Euro-Dollar, etc.) → False
    """
    if (security_type or "").strip() == "144A":
        return True
    # Match " 144A" as a word — avoid false positive on e.g. "S144A" suffixes
    import re
    if re.search(r"\b144A\b", name or "", re.IGNORECASE):
        return True
    return False


def rate_params(api_key: Optional[str]) -> Dict:
    return _WITH_KEY.copy() if api_key else _NO_KEY.copy()
