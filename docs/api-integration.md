# HubSpot Deals API Integration (CRM API v3)

> Verify details against the official docs (https://developers.hubspot.com/docs/api/crm/deals) before submitting;
> rate-limit numbers in particular depend on the HubSpot subscription tier.

## Authentication
Private app access token, sent as `Authorization: Bearer <token>`. Create it in HubSpot:
*Settings → Integrations → Private Apps → Create* ("DLT Deals Extractor"), scope **`crm.objects.deals.read`**.
`401` = invalid/expired token, `403` = scope missing. The service never persists or logs the token; it is passed per scan.

## Endpoint
`GET https://api.hubapi.com/crm/v3/objects/deals`

| Query param | Meaning | Our usage |
|---|---|---|
| `limit` | Page size, max 100 | `HUBSPOT_PAGE_SIZE` (100) |
| `after` | Cursor from `paging.next.after` | checkpoint cursor |
| `properties` | Comma-separated property names; only these are returned (plus defaults) | `DEFAULT_DEAL_PROPERTIES` |
| `archived` | `true` returns archived deals only | `false` |
| `propertiesWithHistory`, `associations` | Optional | not used |

## Response
```json
{
  "results": [
    {
      "id": "512",
      "properties": {
        "dealname": "Closed Won - Initech",
        "amount": "50000",
        "dealstage": "closedwon",
        "pipeline": "default",
        "closedate": "2026-09-30T00:00:00.000Z",
        "createdate": "2026-01-01T09:12:44.123Z",
        "hs_lastmodifieddate": "2026-02-01T10:00:00.000Z",
        "hs_object_id": "512"
      },
      "createdAt": "2026-01-01T09:12:44.123Z",
      "updatedAt": "2026-02-01T10:00:00.000Z",
      "archived": false
    }
  ],
  "paging": { "next": { "after": "513", "link": "https://api.hubapi.com/crm/v3/objects/deals?after=513" } }
}
```
`paging` is absent on the last page. All property values are **strings** (amounts, booleans, dates).

## Pagination
Cursor based: repeat the request with `after=<paging.next.after>` until `paging.next` is missing.

## Rate limits & error handling
| Situation | Behaviour |
|---|---|
| Client-side limit | Sliding window, default 150 requests / 10 s (configurable; HubSpot's real limit depends on tier and is per private app; daily limits also apply) |
| `429` | Wait `Retry-After` (or exponential backoff), retry up to `HUBSPOT_MAX_RETRIES`, then fail the job as rate-limited |
| `5xx`, timeouts, connection errors | Exponential backoff (1, 2, 4… max 30 s), retry, then fail the job (checkpoint kept → resumable) |
| `400` | Not retried (e.g. unknown property) |
| `401` / `403` | Not retried; job `failed` with a clear message |

## Deal properties
Standard properties the service requests: `dealname, amount, dealstage, pipeline, dealtype, description,
closedate, createdate, hs_lastmodifieddate, hubspot_owner_id, hs_is_closed_won, hs_is_closed, hs_priority`.
Other common standard properties: `hs_object_id, hs_forecast_amount, hs_forecast_probability,
hs_deal_stage_probability, hs_acv, hs_arr, hs_mrr, hs_tcv, notes_last_updated, num_associated_contacts,
hs_analytics_source`. **The authoritative, complete list (including custom properties) for your account comes
from** `GET /crm/v3/properties/deals`.

Default pipeline stage IDs: `appointmentscheduled, qualifiedtobuy, presentationscheduled,
decisionmakerboughtin, contractsent, closedwon, closedlost`.
