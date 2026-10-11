# Service API Documentation
Interactive docs: **Swagger UI `/docs`**, ReDoc `/redoc`, OpenAPI `/openapi.json`. Base URL (dev): `http://localhost:5200`.

## Authentication
* Service: if `SERVICE_API_KEY` is set, send `X-API-Key: <key>` on every `/api/v1/*` call (`401` otherwise). `/health` stays open.
* HubSpot: the private-app token goes in the `access_token` field of the start/resume body. It is not stored.

## Endpoints
| Method | Path | Success | Description |
|---|---|---|---|
| GET | `/health`, `/api/v1/health` | 200 | `{"status":"ok"}` (503 if job DB unreachable) |
| POST | `/api/v1/scan/start` | 202 | Start extraction |
| GET | `/api/v1/scan/status/{job_id}` | 200 | Status, counts, checkpoint, error |
| GET | `/api/v1/scan/result/{job_id}?limit=&offset=` | 200 | Extracted deals (job must be `completed`) |
| POST | `/api/v1/scan/cancel/{job_id}` | 200 | Cancel a pending/in-progress job |
| POST | `/api/v1/scan/resume/{job_id}` | 202 | Resume a failed/cancelled job from its checkpoint |
| DELETE | `/api/v1/scan/remove/{job_id}` | 200 | Delete job and its rows |
| GET | `/api/v1/jobs/jobs?limit=&offset=&status=` | 200 | List jobs |
| GET | `/api/v1/jobs/statistics` | 200 | Aggregate statistics |

Job lifecycle: `pending → in_progress → completed | failed | cancelled`.

## Request schemas
`POST /api/v1/scan/start`: `{"access_token": string (10–512 chars, required), "tenant_id": string (default "default", [A-Za-z0-9_.-]+)}`
`POST /api/v1/scan/resume/{job_id}`: `{"access_token": string}`

## Examples
```bash
curl -s localhost:5200/health

# start
curl -s -X POST localhost:5200/api/v1/scan/start -H 'Content-Type: application/json' \
  -d '{"access_token":"pat-na1-xxxxxxxx","tenant_id":"acme"}'
# 202 {"job_id":"3f2c…","status":"pending"}

# status
curl -s localhost:5200/api/v1/scan/status/3f2c…
# {"job_id":"3f2c…","tenant_id":"acme","status":"completed","record_count":5,"pages_done":1,
#  "checkpoint_after":null,"error":null,"start_time":"…","end_time":"…","created_at":"…"}

# results
curl -s "localhost:5200/api/v1/scan/result/3f2c…?limit=100&offset=0"
# {"job_id":"3f2c…","total":5,"limit":100,"offset":0,"next_offset":null,
#  "records":[{"id":"512","dealname":"Closed Won - Initech","amount":"50000.00","dealstage":"closedwon",
#              "closedate":"2026-09-30T00:00:00+00:00","_tenant_id":"acme","_scan_id":"3f2c…", …}]}
```

## Errors
| Code | When |
|---|---|
| 400 | Validation error (missing/short token, bad `tenant_id`, bad `limit`/`status`), malformed JSON, non-UUID `job_id` |
| 401 | Missing/invalid `X-API-Key` |
| 404 | Unknown `job_id` |
| 409 | Results of an unfinished job; cancel of a terminal job; second active scan for a tenant; resume of a non-failed job; remove while in progress |
| 503 | Health check cannot reach the job database |

Error body: `{"detail":"…"}`; validation errors add `"errors":[{"field":"access_token","message":"Field required"}]`.
HubSpot-side failures (invalid token, 429 after retries, 5xx) do not change the HTTP status of the start call; they
appear as `status:"failed"` with a descriptive `error` on the job.
