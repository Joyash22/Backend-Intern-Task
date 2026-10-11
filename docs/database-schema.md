# Database Schema (PostgreSQL)

dlt creates and evolves the table; this is the target shape (types enforced via `COLUMN_HINTS` in
`services/data_source.py`). Schema: `hubspot_deals` (`DATASET_SCHEMA`). dlt also adds `_dlt_load_id` and `_dlt_id`
to the table and manages `_dlt_loads`, `_dlt_pipeline_state` and `_dlt_version`. Job metadata lives in
`public.scan_jobs` (`app/jobs.py`).

## HubSpot → PostgreSQL type mapping
| HubSpot | PostgreSQL | Notes |
|---|---|---|
| object `id` (string) | `TEXT` | part of primary key |
| `string` | `TEXT` | |
| `number` (`amount`) | `NUMERIC(18,2)` | parsed with `Decimal`; invalid → NULL |
| `datetime` / `date` (ISO or epoch ms) | `TIMESTAMPTZ` | normalised to UTC |
| `bool` ("true"/"false") | `BOOLEAN` | |
| `enumeration` (stage, pipeline) | `TEXT` | |

## Reference DDL
```sql
CREATE SCHEMA IF NOT EXISTS hubspot_deals;

CREATE TABLE hubspot_deals.deals (
    id                   TEXT          NOT NULL,
    dealname             TEXT,
    amount               NUMERIC(18,2),
    dealstage            TEXT,
    pipeline             TEXT,
    dealtype             TEXT,
    description          TEXT,
    closedate            TIMESTAMPTZ,
    createdate           TIMESTAMPTZ,
    hs_lastmodifieddate  TIMESTAMPTZ,
    hubspot_owner_id     TEXT,
    hs_is_closed_won     BOOLEAN,
    hs_is_closed         BOOLEAN,
    hs_priority          TEXT,
    archived             BOOLEAN,
    -- ETL metadata
    extracted_at         TIMESTAMPTZ   NOT NULL,
    _scan_id             TEXT          NOT NULL,
    _tenant_id           TEXT          NOT NULL,
    PRIMARY KEY (_tenant_id, id)
);

CREATE INDEX idx_deals_tenant_stage  ON hubspot_deals.deals (_tenant_id, dealstage);
CREATE INDEX idx_deals_tenant_close  ON hubspot_deals.deals (_tenant_id, closedate);
CREATE INDEX idx_deals_tenant_create ON hubspot_deals.deals (_tenant_id, createdate);
CREATE INDEX idx_deals_scan          ON hubspot_deals.deals (_scan_id);
```
dlt creates the table itself (it does not create PRIMARY KEY constraints by default, so add one if you want it enforced); the secondary indexes above are **not** created by dlt – apply them once with
`psql` (see README) for production use.

## Multi-tenant isolation
* Every row carries `_tenant_id`; the merge key is `(id, _tenant_id)`, so two tenants may hold the same HubSpot deal id.
* One dlt pipeline (state and working dir) per tenant: `hubspot_deals_<tenant>`.
* All read/delete queries are scoped by `_scan_id`, which belongs to exactly one tenant.
* Only one active scan per tenant (HTTP 409 otherwise).
* Possible hardening: PostgreSQL Row-Level Security keyed on a session setting such as `app.tenant_id`.

## Load semantics
`write_disposition = merge` on `(id, _tenant_id)`: re-scans update rows instead of duplicating them. A deal's
`_scan_id` always points to the **latest** scan that wrote it, so `GET /scan/result/{old_job}` returns only the rows
that old scan still owns. Checkpoints (`scan_jobs.checkpoint_after`) are written only after a chunk is loaded.
