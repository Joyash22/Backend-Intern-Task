# hubspot-deals-etl

HubSpot CRM **Deals** → PostgreSQL extraction service built with [dlt](https://dlthub.com), FastAPI and Docker.

```
app/            FastAPI app, settings, job store
services/       hubspot_api_service.py · data_source.py (dlt resource) · extraction_service.py
docs/           api-integration.md · database-schema.md · api-documentation.md
tests/          unit, seeded-data, mocked end-to-end and edge-case tests
scripts/        create_test_deals.py (creates the 5 test deals in HubSpot)
test-results/   logs and verification evidence
```

> **Note on the DLT Generator:** the generator repository (`Glynac-Al/Backend-Tools-and-assessment`) is private
> and could not be cloned, so this project structure was written by hand using the names and ports from the task
> (`hubspot-deals-etl`, `hubspot_deals`, ports 5200/5201/5202).

> **Note on authentication:** HubSpot now recommends **Service Keys** instead of legacy Private Apps for
> single-account API access. A Service Key with the deals read scope was used. It gives a Bearer access token,
> which is used exactly like a Private App token.

## Setup (Docker + PostgreSQL)

Requirements: Docker Desktop (running).

```bash
cp .env.example .env        # Windows: copy .env.example .env
docker compose up -d --build
curl http://localhost:5200/health
```

Then open Swagger UI at **http://localhost:5200/docs**.

Port settings: dev `5200`, stage `5201`, prod `5202` (set `SERVICE_PORT` in `.env`).
Do not put your HubSpot token in `.env`. It is sent with each scan request.

## Setup (local, without Docker)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Mac/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
# set DESTINATION=duckdb, then:
python -m uvicorn app.main:app_factory --factory --port 5200
```

## Usage

1. Open http://localhost:5200/docs and use **POST /api/v1/scan/start**, or run:

```bash
curl -X POST localhost:5200/api/v1/scan/start -H "Content-Type: application/json" \
  -d '{"access_token":"<HUBSPOT_ACCESS_TOKEN>","tenant_id":"dev"}'
```

2. Copy the `job_id` from the response.
3. Check progress: `GET /api/v1/scan/status/{job_id}` until `status` is `completed`.
4. Get the deals: `GET /api/v1/scan/result/{job_id}`.
5. Verify in PostgreSQL:

```bash
docker compose exec postgres psql -U dlt_user -d hubspot_etl -c "select id, dealname, amount, dealstage from hubspot_deals.deals order by id;"
```

Leave the `x-api-key` header empty unless you set `SERVICE_API_KEY` in `.env`.
Full endpoint reference: `docs/api-documentation.md`.

## Checkpoints and resume

Extraction runs in chunks of `CHECKPOINT_EVERY_N_PAGES` pages. After each chunk is loaded into the database, the
HubSpot pagination cursor is saved on the job. A failed or cancelled scan can be continued with
`POST /api/v1/scan/resume/{job_id}` from the last saved cursor. Loads use `merge` on `(id, _tenant_id)`, so
re-running never creates duplicates. On service start, jobs that were still running are marked `failed` and can
be resumed.

## Tests

```bash
python -m pip install -r requirements.txt
python -m pytest tests -v
```

46 automated tests (HubSpot mocked, DuckDB destination) cover: HubSpot client pagination, 401/403 handling,
429 with Retry-After, 5xx retries and the rate limiter; data transformation; seeded-data API tests; mocked
end-to-end extraction of 5 deals; checkpoint and resume after a failure; and edge cases (invalid input, unknown
or injected job IDs, 409 conflicts, malformed bodies, API key). Output: `test-results/pytest-output.txt`.

## Live verification (completed)

Run with Docker Compose and PostgreSQL against a real HubSpot test account:

- Service started without errors; `GET /health` returned `{"status":"ok"}`.
- 5 test deals were created with `scripts/create_test_deals.py` (stages: qualified, presentation scheduled,
  closed won, closed lost, qualified; amounts: 5K, 25K, 50K, 75K, 100K). IDs are in `test-results/test-deals.json`.
- Scan `1f212241-5d8a-40be-b705-1480d5e5122b` finished with `status: completed` and `record_count: 5`
  in about 2 seconds. All 5 deals were returned by `/scan/result`.
- Database check of the 5 rows: `test-results/db-verification.txt`.
- Restart test: after `docker compose restart hubspot-deals-etl` the job still reported `completed`
  (`test-results/restart-test.json`).
- Evidence files in `test-results/`: `health-check.txt`, `scan-status.json`, `scan-result.json`,
  `db-verification.txt`, `restart-test.json`, `test-deals.json`, `pytest-output.txt`.

## Known limitations

- Checkpoint and resume were verified by the automated tests, not by interrupting a live scan (5 deals fit in
  a single page, so there is nothing to interrupt).
- `GET /scan/result/{job_id}` returns the rows last written by that scan (merge semantics).
- Orphan recovery at startup assumes a single service process.
- The client-side rate limit defaults to 150 requests per 10 seconds, as in the task. HubSpot's real limit
  depends on the account tier, so adjust it in `.env`.
- Secondary indexes in `docs/database-schema.md` are not created by dlt and must be applied manually.

## Security

The HubSpot access token is never stored or logged and must never be committed. `.env` is git-ignored.
`.env.example` contains only placeholders and development database credentials.
