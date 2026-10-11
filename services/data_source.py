"""dlt resource that extracts HubSpot deals with cursor pagination and chunked checkpoints.

Checkpoint design: the extraction service runs the resource in chunks of N pages
(`max_pages`). The cursor is persisted only AFTER a chunk has been loaded into the
destination, so a crash never skips data. Loads use `merge` on (id, _tenant_id),
making a re-run of a partially loaded chunk idempotent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterator, List, Optional

import dlt

from services.hubspot_api_service import DEFAULT_DEAL_PROPERTIES, HubSpotAPIService

logger = logging.getLogger(__name__)

COLUMN_HINTS = {
    "id": {"data_type": "text", "nullable": False},
    "_tenant_id": {"data_type": "text", "nullable": False},
    "_scan_id": {"data_type": "text", "nullable": False},
    "extracted_at": {"data_type": "timestamp"},
    "amount": {"data_type": "decimal", "precision": 18, "scale": 2},
    "closedate": {"data_type": "timestamp"},
    "createdate": {"data_type": "timestamp"},
    "hs_lastmodifieddate": {"data_type": "timestamp"},
    "hs_is_closed_won": {"data_type": "bool"},
    "hs_is_closed": {"data_type": "bool"},
    "archived": {"data_type": "bool"},
    # explicit text hints so all-NULL columns are still materialised in the destination
    **{c: {"data_type": "text"} for c in ("dealname", "dealstage", "pipeline", "dealtype",
                                           "description", "hubspot_owner_id", "hs_priority")},
}


@dataclass
class ScanContext:
    tenant_id: str
    scan_id: str


@dataclass
class Cursor:
    """Mutable extraction position shared between the resource and the caller."""
    after: Optional[str] = None
    pages: int = 0
    records: int = 0
    exhausted: bool = False


def _to_decimal(value) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        logger.warning("Unparseable amount %r; storing NULL", value)
        return None


def _to_datetime(value) -> Optional[datetime]:
    """HubSpot returns ISO-8601 strings; some date properties are epoch millis."""
    if value in (None, ""):
        return None
    try:
        if str(value).isdigit():
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        logger.warning("Unparseable date %r; storing NULL", value)
        return None


def _to_bool(value) -> Optional[bool]:
    if value in (None, ""):
        return None
    return str(value).lower() == "true"


def transform_deal(raw: dict, ctx: ScanContext, extracted_at: Optional[datetime] = None) -> dict:
    """Map a HubSpot deal object to the flat row described in docs/database-schema.md."""
    p = raw.get("properties", {}) or {}
    return {
        "id": str(raw["id"]),
        "dealname": p.get("dealname"),
        "amount": _to_decimal(p.get("amount")),
        "dealstage": p.get("dealstage"),
        "pipeline": p.get("pipeline"),
        "dealtype": p.get("dealtype"),
        "description": p.get("description"),
        "closedate": _to_datetime(p.get("closedate")),
        "createdate": _to_datetime(p.get("createdate")),
        "hs_lastmodifieddate": _to_datetime(p.get("hs_lastmodifieddate")),
        "hubspot_owner_id": p.get("hubspot_owner_id"),
        "hs_is_closed_won": _to_bool(p.get("hs_is_closed_won")),
        "hs_is_closed": _to_bool(p.get("hs_is_closed")),
        "hs_priority": p.get("hs_priority"),
        "archived": bool(raw.get("archived", False)),
        # ETL metadata
        "extracted_at": extracted_at or datetime.now(timezone.utc),
        "_scan_id": ctx.scan_id,
        "_tenant_id": ctx.tenant_id,
    }


def hubspot_deals_resource(api: HubSpotAPIService, ctx: ScanContext, cursor: Cursor,
                           page_size: int = 100, max_pages: Optional[int] = None,
                           should_cancel: Callable[[], bool] = lambda: False,
                           properties: Optional[List[str]] = None):
    """Build the dlt resource. Advances `cursor` as pages are yielded."""

    @dlt.resource(name="deals", primary_key=["id", "_tenant_id"],
                  write_disposition="merge", columns=COLUMN_HINTS)
    def deals() -> Iterator[List[dict]]:
        fetched = 0
        while True:
            if should_cancel():
                logger.info("Scan %s cancelled; stopping before next page", ctx.scan_id)
                return
            page = api.get_deals_page(cursor.after, properties or DEFAULT_DEAL_PROPERTIES, page_size)
            rows = [transform_deal(r, ctx) for r in page.results]
            if rows:
                yield rows
            cursor.records += len(rows)
            cursor.pages += 1
            cursor.after = page.next_after
            fetched += 1
            if not page.next_after:
                cursor.exhausted = True
                return
            if max_pages and fetched >= max_pages:
                return

    return deals
