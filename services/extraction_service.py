"""Orchestrates scans: validates credentials, runs the dlt pipeline in checkpointed chunks."""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import dlt

from app.config import Settings
from app.jobs import ACTIVE, JobStore, now
from services.data_source import Cursor, ScanContext, hubspot_deals_resource
from services.hubspot_api_service import HubSpotAPIError, HubSpotAPIService

logger = logging.getLogger(__name__)

RESULT_COLUMNS = ["id", "dealname", "amount", "dealstage", "pipeline", "dealtype", "description",
                  "closedate", "createdate", "hs_lastmodifieddate", "hubspot_owner_id",
                  "hs_is_closed_won", "hs_is_closed", "hs_priority", "archived",
                  "extracted_at", "_scan_id", "_tenant_id"]


class ConflictError(Exception):
    pass


class ExtractionService:
    def __init__(self, settings: Settings, store: JobStore,
                 api_factory: Optional[Callable[[str], HubSpotAPIService]] = None,
                 executor: Optional[ThreadPoolExecutor] = None):
        self.settings, self.store = settings, store
        self._api_factory = api_factory or self._default_api_factory
        self._executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="scan")

    def _default_api_factory(self, token: str) -> HubSpotAPIService:
        s = self.settings
        return HubSpotAPIService(token, s.hubspot_base_url, s.hubspot_timeout, s.hubspot_max_retries,
                                 s.rate_limit_requests, s.rate_limit_window)

    # --------------------------------------------------------------- pipeline
    def _destination(self):
        if self.settings.destination == "postgres":
            return dlt.destinations.postgres(credentials=self.settings.postgres_url)
        return dlt.destinations.duckdb(self.settings.duckdb_path)

    def pipeline(self, tenant_id: str) -> dlt.Pipeline:
        safe = re.sub(r"[^a-zA-Z0-9_]", "_", tenant_id)
        return dlt.pipeline(pipeline_name=f"{self.settings.pipeline_name}_{safe}",
                            destination=self._destination(),
                            dataset_name=self.settings.dataset_schema,
                            pipelines_dir=self.settings.pipelines_dir)

    # ------------------------------------------------------------------ jobs
    def start(self, tenant_id: str, access_token: str) -> dict:
        if self.store.active_for_tenant(tenant_id):
            raise ConflictError(f"A scan is already running for tenant '{tenant_id}'")
        job = self.store.create(tenant_id)
        self._executor.submit(self.run_job, job["job_id"], access_token)
        return job

    def resume(self, job_id: str, access_token: str) -> dict:
        """Continue a failed/cancelled job from its last durable checkpoint."""
        job = self.store.get(job_id)
        if job["status"] not in ("failed", "cancelled"):
            raise ConflictError(f"Only failed or cancelled jobs can be resumed (status={job['status']})")
        if self.store.active_for_tenant(job["tenant_id"]):
            raise ConflictError("Another scan is already running for this tenant")
        self.store.update(job_id, status="pending", cancel_requested=False, error=None, end_time=None)
        self._executor.submit(self.run_job, job_id, access_token)
        return self.store.get(job_id)

    def run_job(self, job_id: str, access_token: str) -> None:
        job = self.store.get(job_id)
        if not job:
            return
        self.store.update(job_id, status="in_progress", start_time=job["start_time"] or now())
        ctx = ScanContext(tenant_id=job["tenant_id"], scan_id=job_id)
        cursor = Cursor(after=job["checkpoint_after"], pages=job["pages_done"], records=job["record_count"])
        try:
            api = self._api_factory(access_token)
            api.validate_credentials()
            pipeline = self.pipeline(ctx.tenant_id)
            chunk = max(self.settings.checkpoint_every_n_pages, 1)
            cancelled = lambda: bool((self.store.get(job_id) or {}).get("cancel_requested"))  # noqa: E731

            while not cursor.exhausted:
                if cancelled():
                    break
                resource = hubspot_deals_resource(api, ctx, cursor, self.settings.hubspot_page_size,
                                                  max_pages=chunk, should_cancel=cancelled)
                pipeline.run(resource)
                # Chunk is durably loaded -> persist the checkpoint.
                self.store.update(job_id, checkpoint_after=cursor.after, pages_done=cursor.pages,
                                  record_count=cursor.records)
                logger.info("Scan %s checkpoint: pages=%d records=%d after=%s",
                            job_id, cursor.pages, cursor.records, cursor.after)

            final = "cancelled" if (cancelled() and not cursor.exhausted) else "completed"
            self.store.update(job_id, status=final, end_time=now())
            logger.info("Scan %s finished with status=%s records=%d", job_id, final, cursor.records)
        except HubSpotAPIError as exc:
            logger.error("Scan %s failed (HubSpot %s): %s", job_id, exc.status_code, exc.message)
            self.store.update(job_id, status="failed", end_time=now(), error=exc.message)
        except Exception as exc:  # noqa: BLE001 - job must always reach a terminal state
            logger.exception("Scan %s failed unexpectedly", job_id)
            self.store.update(job_id, status="failed", end_time=now(), error=f"{exc.__class__.__name__}: {exc}")

    # --------------------------------------------------------------- results
    def fetch_results(self, job: dict, limit: int, offset: int) -> tuple[list[dict], int]:
        """Rows last written by this scan (merge semantics: a later scan takes ownership of a deal)."""
        pipeline = self.pipeline(job["tenant_id"])
        try:
            with pipeline.sql_client() as client:
                table = client.make_qualified_table_name("deals")
                total = client.execute_sql(f"SELECT COUNT(*) FROM {table} WHERE _scan_id = %s", job["job_id"])[0][0]
                cols = ", ".join(f'"{c}"' for c in RESULT_COLUMNS)
                rows = client.execute_sql(
                    f"SELECT {cols} FROM {table} WHERE _scan_id = %s ORDER BY id LIMIT {int(limit)} OFFSET {int(offset)}",
                    job["job_id"])
        except Exception as exc:  # table/dataset not created yet -> zero deals
            logger.info("No result table yet for tenant %s (%s)", job["tenant_id"], exc.__class__.__name__)
            return [], 0
        out = []
        for r in rows:
            d = dict(zip(RESULT_COLUMNS, r))
            for k, v in d.items():
                if hasattr(v, "isoformat"):
                    d[k] = v.isoformat()
                elif v.__class__.__name__ == "Decimal":
                    d[k] = str(v)
            out.append(d)
        return out, total

    def remove_results(self, job: dict) -> None:
        try:
            with self.pipeline(job["tenant_id"]).sql_client() as client:
                table = client.make_qualified_table_name("deals")
                client.execute_sql(f"DELETE FROM {table} WHERE _scan_id = %s", job["job_id"])
        except Exception as exc:
            logger.info("Nothing to remove for job %s (%s)", job["job_id"], exc.__class__.__name__)
