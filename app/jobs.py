"""Persistent scan-job store (SQLAlchemy Core; Postgres in Docker, SQLite locally)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (Boolean, Column, DateTime, Integer, MetaData, String, Table, Text,
                        create_engine, delete, func, insert, select, update)

ACTIVE = ("pending", "in_progress")
STATUSES = ("pending", "in_progress", "completed", "failed", "cancelled")

metadata = MetaData()
scan_jobs = Table(
    "scan_jobs", metadata,
    Column("job_id", String(36), primary_key=True),
    Column("tenant_id", String(128), nullable=False, index=True),
    Column("status", String(20), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("start_time", DateTime(timezone=True)),
    Column("end_time", DateTime(timezone=True)),
    Column("record_count", Integer, nullable=False, default=0),
    Column("pages_done", Integer, nullable=False, default=0),
    Column("checkpoint_after", String(256)),
    Column("error", Text),
    Column("cancel_requested", Boolean, nullable=False, default=False),
)


def now() -> datetime:
    return datetime.now(timezone.utc)


class JobStore:
    def __init__(self, url: str):
        kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, future=True, **kwargs)
        metadata.create_all(self.engine)

    def create(self, tenant_id: str, status: str = "pending", **fields) -> dict:
        job_id = fields.pop("job_id", str(uuid.uuid4()))
        row = dict(job_id=job_id, tenant_id=tenant_id, status=status, created_at=now(),
                   record_count=0, pages_done=0, cancel_requested=False)
        row.update(fields)
        with self.engine.begin() as c:
            c.execute(insert(scan_jobs).values(**row))
        return self.get(job_id)

    def get(self, job_id: str) -> Optional[dict]:
        with self.engine.connect() as c:
            r = c.execute(select(scan_jobs).where(scan_jobs.c.job_id == job_id)).mappings().first()
        return dict(r) if r else None

    def update(self, job_id: str, **fields) -> None:
        with self.engine.begin() as c:
            c.execute(update(scan_jobs).where(scan_jobs.c.job_id == job_id).values(**fields))

    def delete(self, job_id: str) -> None:
        with self.engine.begin() as c:
            c.execute(delete(scan_jobs).where(scan_jobs.c.job_id == job_id))

    def active_for_tenant(self, tenant_id: str) -> Optional[dict]:
        with self.engine.connect() as c:
            r = c.execute(select(scan_jobs).where(scan_jobs.c.tenant_id == tenant_id,
                                                  scan_jobs.c.status.in_(ACTIVE))).mappings().first()
        return dict(r) if r else None

    def list(self, limit: int = 50, offset: int = 0, status: Optional[str] = None) -> tuple[List[dict], int]:
        q = select(scan_jobs)
        cq = select(func.count()).select_from(scan_jobs)
        if status:
            q, cq = q.where(scan_jobs.c.status == status), cq.where(scan_jobs.c.status == status)
        q = q.order_by(scan_jobs.c.created_at.desc()).limit(limit).offset(offset)
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q).mappings()], c.execute(cq).scalar_one()

    def fail_orphans(self) -> int:
        """On startup: jobs still pending/in_progress lost their worker -> mark failed so they can be resumed."""
        with self.engine.begin() as c:
            r = c.execute(update(scan_jobs).where(scan_jobs.c.status.in_(ACTIVE)).values(
                status="failed", end_time=now(), error="Interrupted by service restart; resume to continue"))
        return r.rowcount

    def statistics(self) -> dict:
        with self.engine.connect() as c:
            rows = c.execute(select(scan_jobs)).mappings().all()
        by_status = {s: 0 for s in STATUSES}
        durations, records = [], 0
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            records += r["record_count"] or 0
            if r["status"] == "completed" and r["start_time"] and r["end_time"]:
                durations.append((r["end_time"] - r["start_time"]).total_seconds())
        return {
            "total_jobs": len(rows),
            "by_status": by_status,
            "total_records_extracted": records,
            "average_extraction_seconds": round(sum(durations) / len(durations), 3) if durations else None,
        }
