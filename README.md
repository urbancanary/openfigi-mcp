# OpenFIGI MCP

FastAPI service that wraps the [OpenFIGI](https://www.openfigi.com/) free API to map ISINs to
Bloomberg FIGI identifiers, parse exact fractional coupons from Bloomberg names
(e.g. `4 3/8` → `4.375`), and write results to a Supabase `bond_reference` table.

---

## Quick Start

```bash
# Install
pip install -r requirements.txt

# Run
AUTH_MCP_URL=https://auth-mcp.example.com uvicorn server:app --port 8080
```

**The service will not start without `AUTH_MCP_URL`** — it is checked at import time
(`supabase_writer.py:21-23`). All other secrets are resolved at runtime through
auth-mcp (see Credentials below).

---

## Credentials

Secrets are fetched from the [auth-mcp](https://github.com/andyseaman/auth-mcp) service at
`{AUTH_MCP_URL}/api/key/{name}`. If auth-mcp is unreachable or returns empty, the
module falls back to the corresponding environment variable.

| Auth-mcp key                    | Env var fallback          | Used for                              |
|----------------------------------|---------------------------|---------------------------------------|
| `OPENFIGI_API_KEY`               | `OPENFIGI_API_KEY`        | OpenFIGI API (authenticated rate-limit) |
| `BOND_DATA_SUPABASE_URL`         | `SUPABASE_URL`            | Supabase project URL                   |
| `BOND_DATA_SUPABASE_KEY`         | `SUPABASE_KEY`            | Supabase service-role key              |

OpenFIGI API keys are free at [openfigi.com/api](https://www.openfigi.com/api). Without a key
the service still works but is rate-limited to 10 ISINs/request at 20 req/min.

---

## Endpoints

### `GET /health`
Service health check — used by Railway/Coolify.

### `GET /lookup/{isin}`
Single-ISIN lookup. Does **not** write to the database. Returns FIGI, Bloomberg name, ticker,
market sector, security type, and parsed fractional coupon.

### `POST /enrich`
Batch-enrich `bond_reference` with OpenFIGI data. Processes ISINs that have not yet been checked
(or a specific list passed in the body).

- Writes OpenFIGI static fields to `bond_reference`
- Writes `coupon_bbg` (the Bloomberg-parsed fractional coupon) alongside the existing `coupon`
- **Does NOT update the `coupon` column** — promotion is deferred (see Coupon Contract below)
- Pass `dry_run: true` to preview without writing

### `GET /coupon-upgrades`
List bonds where `coupon_bbg` differs materially from `coupon` (default delta > 0.001).
These are candidates for manual correction.

### `GET /brian-manifest`
Brian-engine service registry metadata.

---

## Coupon Contract

The `/enrich` endpoint **writes the parsed Bloomberg coupon to `coupon_bbg`**, not to `coupon`.
The existing `coupon` column is left untouched. The comment in the code reads "let DB compare."

Why defer?

- The bond may have a `bond_identity` lock that preserves an authoritative coupon value.
- The correction may need manual review before it is applied to production analytics.
- Some consumers already read `coupon_bbg` directly.

To find candidates for promotion, call `GET /coupon-upgrades`. Once reviewed, the correct
coupon value can be written to the `coupon` column by an external process.

---

## Consumers

| Consumer      | Integration                         |
|---------------|-------------------------------------|
| **ga10-pricing** | Calls `GET /lookup` (via `hopper.js`) for ad-hoc ISIN lookups |
| **etf-scraper** | Contains a duplicated (legacy) OpenFIGI enricher — should be migrated to this service |

---

## Deploy

- **Platform**: Railway (project: `openfigi-mcp-production`)
- **Build**: Dockerfile (python:3.12-slim, uvicorn on port 8080)
- **Healthcheck**: `GET /health` via `railway.toml`
- **Live URL**: `https://openfigi-mcp-production.up.railway.app`

Environment variables are injected by Railway; `AUTH_MCP_URL` must point to a reachable
auth-mcp instance.
