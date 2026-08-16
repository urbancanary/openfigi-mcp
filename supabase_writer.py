"""
Minimal Supabase REST writer for openfigi-mcp.

Credentials are fetched once at startup from auth-mcp and cached in module state.
"""

from __future__ import annotations

import logging
import os
import random
import time
from datetime import date
from typing import Any, Callable, Dict, List, Optional

import requests

from token_utils import generate_token

logger = logging.getLogger("openfigi-mcp.supabase")

_MAX_ATTEMPTS = 3
_BASE_DELAY_S = 1.5


def _with_retry(fn: Callable[[], requests.Response], desc: str) -> requests.Response:
    """
    Retry a Supabase REST call up to _MAX_ATTEMPTS times with exponential
    backoff on 5xx/timeout/connection errors. Escalates to logger.error
    (not just warning) once retries are exhausted, so a persistent failure
    is visible rather than a single warning string buried in Railway
    logs (#1494).
    """
    last_exc: Optional[Exception] = None
    resp: Optional[requests.Response] = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = fn()
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            resp = None
        else:
            last_exc = None
            if resp.status_code < 500:
                return resp

        if attempt == _MAX_ATTEMPTS - 1:
            break
        delay = _BASE_DELAY_S * (2 ** attempt) * (0.5 + random.random())
        reason = f"HTTP {resp.status_code}" if resp is not None else repr(last_exc)
        logger.warning(f"{desc} failed ({reason}) — attempt {attempt + 1}/{_MAX_ATTEMPTS}, retrying in {delay:.1f}s")
        time.sleep(delay)

    if last_exc is not None:
        logger.error(f"{desc} failed after {_MAX_ATTEMPTS} attempts: {last_exc}")
        raise last_exc
    logger.error(f"{desc} failed after {_MAX_ATTEMPTS} attempts: HTTP {resp.status_code}")
    return resp

AUTH_MCP_URL = os.environ.get("AUTH_MCP_URL", "")
if not AUTH_MCP_URL:
    raise RuntimeError("Configuration missing")

_cfg: Dict[str, str] = {}


def _get_key(name: str) -> str:
    """
    Fetch a key from auth-mcp.

    Does NOT silently fall back to os.environ — a missing/unreachable
    auth-mcp must be visible (log + raise), not masked, since a silent
    fallback here previously caused OPENFIGI_API_KEY lookups to downgrade
    to unauthenticated OpenFIGI mode (10 ISINs/req, 20 req/min vs
    100/240 — a ~100x throughput drop) with zero signal anywhere (#1484).
    """
    try:
        resp = requests.get(
            f"{AUTH_MCP_URL}/api/key/{name}",
            headers={"Authorization": f"Bearer {generate_token()}"},
            timeout=5,
        )
    except Exception as e:
        logger.error(f"auth-mcp unreachable fetching key {name!r}: {e}")
        raise RuntimeError(f"auth-mcp unreachable fetching key {name!r}") from e

    if resp.status_code != 200:
        logger.error(f"auth-mcp returned {resp.status_code} fetching key {name!r}")
        raise RuntimeError(f"auth-mcp returned {resp.status_code} fetching key {name!r}")

    val = resp.json().get("value", "")
    if not val:
        logger.error(f"auth-mcp has no value for key {name!r}")
        raise RuntimeError(f"auth-mcp has no value for key {name!r}")
    return val


def _ensure_config() -> None:
    if not _cfg.get("key"):
        _cfg["url"] = _get_key("BOND_DATA_SUPABASE_URL") or _get_key("SUPABASE_URL")
        _cfg["key"] = _get_key("BOND_DATA_SUPABASE_KEY") or _get_key("SUPABASE_KEY")
        if not _cfg.get("key"):
            raise RuntimeError(
                "Supabase credentials not available — "
                "check BOND_DATA_SUPABASE_KEY / BOND_DATA_SUPABASE_URL in auth-mcp"
            )


def _headers(prefer: str = "") -> Dict[str, str]:
    h = {
        "apikey": _cfg["key"],
        "Authorization": f"Bearer {_cfg['key']}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _rest(path: str) -> str:
    return f"{_cfg['url']}/rest/v1/{path}"


# ── Public API ─────────────────────────────────────────────────────────────

def get_rows(table: str, params: Dict[str, str], page_size: int = 1000) -> List[Dict]:
    """
    Paginated SELECT from a Supabase table.

    Orders by `isin` (falling back to whatever `params["order"]` the caller
    supplies) so limit/offset pages are stable — without an ORDER BY,
    Postgres is free to return rows in different order per query, which
    silently skips or duplicates rows across page boundaries (#1488).
    """
    _ensure_config()
    order = params.get("order", "isin")
    out: List[Dict] = []
    offset = 0
    while True:
        p = {**params, "order": order, "limit": str(page_size), "offset": str(offset)}
        resp = _with_retry(
            lambda p=p: requests.get(_rest(table), headers=_headers(), params=p, timeout=30),
            desc=f"get_rows({table})",
        )
        if not resp.ok:
            resp.raise_for_status()
        rows = resp.json()
        out.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size
    return out


def upsert_rows(table: str, rows: List[Dict], on_conflict: str = "isin") -> int:
    """Bulk upsert rows into a Supabase table. Returns rows written."""
    if not rows:
        return 0
    _ensure_config()

    batch_size = 200
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        try:
            resp = _with_retry(
                lambda batch=batch: requests.post(
                    f"{_rest(table)}?on_conflict={on_conflict}",
                    headers=_headers(prefer="resolution=merge-duplicates,return=minimal"),
                    json=batch,
                    timeout=30,
                ),
                desc=f"upsert_rows({table})",
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.error(f"upsert {table} batch dropped after retries: {e}")
            continue
        if resp.ok:
            total += len(batch)
        else:
            logger.error(f"upsert {table} batch failed: {resp.status_code} {resp.text[:200]}")
    return total


def get_single(table: str, isin: str) -> Optional[Dict]:
    """Fetch a single row by ISIN."""
    _ensure_config()
    resp = _with_retry(
        lambda: requests.get(
            _rest(table),
            headers=_headers(),
            params={"isin": f"eq.{isin}", "limit": "1"},
            timeout=10,
        ),
        desc=f"get_single({table})",
    )
    if not resp.ok:
        resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None
