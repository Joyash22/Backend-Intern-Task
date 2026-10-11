"""FastAPI service exposing the HubSpot deals extraction API (Swagger UI at /docs)."""
from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import Settings
from app.jobs import ACTIVE, STATUSES, JobStore
from services.extraction_service import ConflictError, ExtractionService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
TENANT_RE = r"^[A-Za-z0-9_.-]+$"


class StartScanRequest(BaseModel):
    access_token: str = Field(..., min_length=10, max_length=512,
                              description="HubSpot private app access token (scope crm.objects.deals.read). Never stored.")
    tenant_id: str = Field("default", min_length=1, max_length=128, pattern=TENANT_RE)


class ResumeRequest(BaseModel):
    access_token: str = Field(..., min_length=10, max_length=512)


def _public(job: dict) -> dict:
    keys = ["job_id", "tenant_id", "status", "created_at", "start_time", "end_time",
            "record_count", "pages_done", "checkpoint_after", "error"]
    return {k: job[k] for k in keys}


def create_app(settings: Optional[Settings] = None, store: Optional[JobStore] = None,
               service: Optional[ExtractionService] = None) -> FastAPI:
    settings = settings or Settings()
    store = store or JobStore(settings.jobs_database_url)
    service = service or ExtractionService(settings, store)
    store.fail_orphans()

    app = FastAPI(title="HubSpot Deals ETL Service", version="1.0.0",
                  description="Extracts HubSpot CRM deals into PostgreSQL using dlt.")

    def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
        if settings.service_api_key and x_api_key != settings.service_api_key:
            raise HTTPException(401, "Missing or invalid X-API-Key")

    def get_job_or_404(job_id: str) -> dict:
        if not UUID_RE.match(job_id):
            raise HTTPException(400, "job_id must be a valid UUID")
        job = store.get(job_id)
        if not job:
            raise HTTPException(404, f"Job '{job_id}' not found")
        return job

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_, exc: RequestValidationError):
        errors = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse(status_code=400, content={"detail": "Validation error", "errors": errors})

    @app.get("/health", tags=["health"])
    @app.get("/api/v1/health", tags=["health"])
    def health():
        try:
            store.statistics()
            return {"status": "ok", "service": "hubspot_deals", "environment": settings.app_env}
        except Exception:
            return JSONResponse(status_code=503, content={"status": "unhealthy"})

    api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])

    @api.post("/scan/start", status_code=202, tags=["scan"])
    def start_scan(body: StartScanRequest):
        try:
            job = service.start(body.tenant_id, body.access_token)
        except ConflictError as exc:
            raise HTTPException(409, str(exc))
        return {"job_id": job["job_id"], "status": job["status"]}

    @api.get("/scan/status/{job_id}", tags=["scan"])
    def scan_status(job_id: str):
        return _public(get_job_or_404(job_id))

    @api.get("/scan/result/{job_id}", tags=["scan"])
    def scan_result(job_id: str, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        job = get_job_or_404(job_id)
        if job["status"] != "completed":
            raise HTTPException(409, f"Job is '{job['status']}', results are available only when completed")
        rows, total = service.fetch_results(job, limit, offset)
        nxt = offset + limit if offset + limit < total else None
        return {"job_id": job_id, "total": total, "limit": limit, "offset": offset,
                "next_offset": nxt, "records": rows}

    @api.post("/scan/cancel/{job_id}", tags=["scan"])
    def scan_cancel(job_id: str):
        job = get_job_or_404(job_id)
        if job["status"] not in ACTIVE:
            raise HTTPException(409, f"Job cannot be cancelled in status '{job['status']}'")
        store.update(job_id, cancel_requested=True)
        if job["status"] == "pending":
            store.update(job_id, status="cancelled")
        return {"job_id": job_id, "status": "cancelled" if job["status"] == "pending" else "cancelling",
                "message": "Cancellation requested"}

    @api.post("/scan/resume/{job_id}", status_code=202, tags=["scan"])
    def scan_resume(job_id: str, body: ResumeRequest):
        job = get_job_or_404(job_id)
        try:
            return _public(service.resume(job["job_id"], body.access_token))
        except ConflictError as exc:
            raise HTTPException(409, str(exc))

    @api.delete("/scan/remove/{job_id}", tags=["scan"])
    def scan_remove(job_id: str):
        job = get_job_or_404(job_id)
        if job["status"] == "in_progress":
            raise HTTPException(409, "Cancel the job before removing it")
        service.remove_results(job)
        store.delete(job_id)
        return {"job_id": job_id, "message": "Job and extracted data removed"}

    @api.get("/jobs/jobs", tags=["jobs"])
    def list_jobs(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                  status: Optional[str] = Query(None)):
        if status and status not in STATUSES:
            raise HTTPException(400, f"status must be one of {STATUSES}")
        jobs, total = store.list(limit, offset, status)
        return {"total": total, "limit": limit, "offset": offset, "jobs": [_public(j) for j in jobs]}

    @api.get("/jobs/statistics", tags=["jobs"])
    def job_statistics():
        return store.statistics()

    app.include_router(api)
    app.state.service, app.state.store = service, store
    return app


def app_factory() -> FastAPI:  # uvicorn app.main:app_factory --factory
    return create_app()
