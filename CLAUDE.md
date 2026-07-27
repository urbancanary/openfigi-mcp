# OpenFIGI MCP

## Project Structure

| File                | Responsibility                                                  |
|---------------------|-----------------------------------------------------------------|
| `server.py`         | FastAPI application, endpoint handlers, orchestration           |
| `supabase_writer.py`| Supabase REST client; resolves creds from auth-mcp at runtime   |
| `openfigi_client.py`| OpenFIGI v3 `/mapping` HTTP client                              |
| `coupon_parser.py`  | Parses `"4 3/8"` → `4.375` from Bloomberg security names        |
| `test_coupon_parser.py` | Unit tests for coupon parser                               |
| `Dockerfile`        | python:3.12-slim, uvicorn on port 8080, includes curl           |
| `railway.toml`      | Railway deploy config (build + healthcheck)                     |
| `requirements.txt`  | fastapi, uvicorn, pydantic, requests                            |

## Credential Resolution

All secrets go through `supabase_writer._get_key(name)`:

1. Fetch from `{AUTH_MCP_URL}/api/key/{name}` with bearer token
2. On failure → `os.environ.get(name, "")`

`AUTH_MCP_URL` is read at **import time** (`supabase_writer.py:21`); if unset the module
raises `RuntimeError("Configuration missing")`.

**Key names** resolved:
- `OPENFIGI_API_KEY` — OpenFIGI v3 API key
- `BOND_DATA_SUPABASE_URL` (fallback `SUPABASE_URL`)
- `BOND_DATA_SUPABASE_KEY` (fallback `SUPABASE_KEY`)

## Coupon Contract (the actual behavior)

This is the most common source of confusion with this service:

- **`POST /enrich`** writes `coupon_bbg` (the fractionally-precise value) but **never** sets `coupon`.
  The `coupon` column must be updated by an external process after review.
- **`GET /coupon-upgrades`** lists rows where `|coupon_bbg - coupon| > 0.001`.
  This is a **read-only report** — it does not mutate any data.
- `coupon_precision_gain()` at `coupon_parser.py:118` is the predicate used to flag upgrades.
- The `_write_hits` comment `# always store parsed; let DB compare` (server.py:160) is
  misleading to operators — "let DB compare" means "store in a separate column so a human
  can compare later," not "the database will reconcile this."

## Thresholds & Constants

- Coupon precision gain threshold: `> 0.001` (coupon_parser.py:128)
- Recheck interval for bonds with null figi: 30 days (server.py:98)
- Batch sizes: 100 ISINs (authenticated) / 10 (unauthenticated) (openfigi_client.py:28-29)
- Upsert batch: 200 rows (supabase_writer.py:102)
- Rate limits: 240 req/min (auth'd) / 20 req/min (anon) (openfigi_client.py:28-29)

## Design Conventions

- `logging` with named loggers (`openfigi-mcp.*`), not `print`
- Supabase upserts use `?on_conflict=isin` with `Prefer: resolution=merge-duplicates`
- OpenFIGI responses prefer the composite-level hit (`figi == compositeFIGI`) over share-level
- Pydantic models for request/response schemas
- All date stamps use `date.today().isoformat()` (YYYY-MM-DD)

## Consumers

- **ga10-pricing** → `GET /lookup` via hopper.js
- **etf-scraper** → contains a duplicate, older OpenFIGI enricher module

## Deploy

- **Platform**: Railway (`openfigi-mcp-production`)
- **URL**: `https://openfigi-mcp-production.up.railway.app`
- **Healthcheck**: `GET /health` (port 8080)
- **Dockerfile**: python:3.12-slim + curl (for Coolify-style healthchecks)
