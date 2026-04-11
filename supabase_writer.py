"""
Minimal Supabase REST writer for openfigi-mcp.

Credentials are fetched once at startup from auth-mcp and cached in module state.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from datetime import date
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("openfigi-mcp.supabase")

AUTH_MCP_URL = os.environ.get("AUTH_MCP_URL", "https://auth-mcp.urbancanary.workers.dev")

_cfg: Dict[str, str] = {}


def _token() -> str:
    r = secrets.token_hex(8)
    return f"{r}-{hashlib.sha256(r.encode()).hexdigest()[:8]}"


def _get_key(name: str) -> str:
    """Fetch from auth-mcp, fall back to env var."""
    try:
        resp = requests.get(
            f"{AUTH_MCP_URL}/api/key/{name}",
            headers={"Authorization": f"Bearer {_token()}"},
            timeout=5,
        )
        if resp.status_code == 200:
            val = resp.json().get("value", "")
            if val:
                return val
    except Exception:
        pass
    return os.environ.get(name, "")


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
    """Paginated SELECT from a Supabase table."""
    _ensure_config()
    out: List[Dict] = []
    offset = 0
    while True:
        p = {**params, "limit": str(page_size), "offset": str(offset)}
        resp = requests.get(_rest(table), headers=_headers(), params=p, timeout=30)
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
        resp = requests.post(
            f"{_rest(table)}?on_conflict={on_conflict}",
            headers=_headers(prefer="resolution=merge-duplicates,return=minimal"),
            json=batch,
            timeout=30,
        )
        if resp.ok:
            total += len(batch)
        else:
            logger.warning(f"upsert {table} batch failed: {resp.status_code} {resp.text[:200]}")
    return total


def get_single(table: str, isin: str) -> Optional[Dict]:
    """Fetch a single row by ISIN."""
    _ensure_config()
    resp = requests.get(
        _rest(table),
        headers=_headers(),
        params={"isin": f"eq.{isin}", "limit": "1"},
        timeout=10,
    )
    if not resp.ok:
        resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None
