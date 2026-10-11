"""Seeded-data tests (no HubSpot calls) and edge cases per TEST-GUIDELINES-V1."""
import uuid
from datetime import timedelta

import pytest

from app.jobs import now
from services.data_source import COLUMN_HINTS, ScanContext, transform_deal
from tests.conftest import SAMPLE, TOKEN, hs_deal


@pytest.fixture
def seeded(store, service):
    t = now()
    done = store.create("acme", "completed", start_time=t, end_time=t + timedelta(seconds=4), record_count=5)
    pending = store.create("acme", "pending")
    failed = store.create("beta", "failed", error="x")
    cancelled = store.create("beta", "cancelled")
    rows = [transform_deal(hs_deal(*d), ScanContext("acme", done["job_id"])) for d in SAMPLE]
    service.pipeline("acme").run(rows, table_name="deals", write_disposition="merge", primary_key=["id", "_tenant_id"],
                             columns=COLUMN_HINTS)
    return dict(done=done, pending=pending, failed=failed, cancelled=cancelled)


# ------------------------------------------------------------- seeded tests
def test_health_both_paths(client):
    for p in ("/health", "/api/v1/health"):
        assert client.get(p).json()["status"] == "ok"


def test_status_matches_seed(client, seeded):
    j = client.get(f"/api/v1/scan/status/{seeded['done']['job_id']}").json()
    assert j["status"] == "completed" and j["record_count"] == 5 and j["start_time"] and j["end_time"]


def test_result_for_seeded_completed_job(client, seeded):
    r = client.get(f"/api/v1/scan/result/{seeded['done']['job_id']}").json()
    assert r["total"] == 5 and sorted(x["id"] for x in r["records"]) == ["101", "102", "103", "104", "105"]


def test_list_jobs_and_filter(client, seeded):
    assert client.get("/api/v1/jobs/jobs").json()["total"] == 4
    f = client.get("/api/v1/jobs/jobs", params={"status": "failed"}).json()
    assert f["total"] == 1 and f["jobs"][0]["status"] == "failed"
    assert len(client.get("/api/v1/jobs/jobs", params={"limit": 2}).json()["jobs"]) == 2


def test_statistics(client, seeded):
    s = client.get("/api/v1/jobs/statistics").json()
    assert s["total_jobs"] == 4 and s["by_status"]["completed"] == 1 and s["by_status"]["pending"] == 1
    assert s["total_records_extracted"] == 5 and s["average_extraction_seconds"] == 4.0


def test_cancel_pending_job(client, seeded):
    jid = seeded["pending"]["job_id"]
    assert client.post(f"/api/v1/scan/cancel/{jid}").status_code == 200
    assert client.get(f"/api/v1/scan/status/{jid}").json()["status"] == "cancelled"


def test_remove_seeded_job(client, seeded):
    jid = seeded["done"]["job_id"]
    assert client.delete(f"/api/v1/scan/remove/{jid}").status_code == 200
    assert client.get(f"/api/v1/scan/status/{jid}").status_code == 404


# --------------------------------------------------------------- edge cases
@pytest.mark.parametrize("body", [{}, {"access_token": ""}, {"access_token": "short"},
                                  {"access_token": TOKEN, "tenant_id": "bad tenant!"}])
def test_start_validation_errors(client, body):
    r = client.post("/api/v1/scan/start", json=body)
    assert r.status_code == 400 and r.json()["errors"]


def test_malformed_json_body(client):
    r = client.post("/api/v1/scan/start", content="{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


@pytest.mark.parametrize("method,path", [("get", "status"), ("get", "result"), ("post", "cancel"), ("delete", "remove")])
def test_unknown_job_is_404(client, method, path):
    assert getattr(client, method)(f"/api/v1/scan/{path}/{uuid.uuid4()}").status_code == 404


@pytest.mark.parametrize("bad", ["abc", "1; DROP TABLE scan_jobs;--", "a" * 300, "' OR '1'='1"])
def test_id_injection_rejected(client, store, bad):
    r = client.get(f"/api/v1/scan/status/{bad}")
    assert r.status_code in (400, 404)
    assert store.statistics()["total_jobs"] == 0  # table intact


def test_results_for_incomplete_job_409(client, seeded):
    assert client.get(f"/api/v1/scan/result/{seeded['pending']['job_id']}").status_code == 409


@pytest.mark.parametrize("key", ["done", "failed", "cancelled"])
def test_cancel_terminal_job_409(client, seeded, key):
    assert client.post(f"/api/v1/scan/cancel/{seeded[key]['job_id']}").status_code == 409


def test_concurrent_start_same_tenant_409(client, seeded):
    assert client.post("/api/v1/scan/start", json={"access_token": TOKEN, "tenant_id": "acme"}).status_code == 409


def test_resume_only_failed_or_cancelled(client, seeded):
    r = client.post(f"/api/v1/scan/resume/{seeded['done']['job_id']}", json={"access_token": TOKEN})
    assert r.status_code == 409


def test_bad_pagination_and_status_filter(client, seeded):
    jid = seeded["done"]["job_id"]
    assert client.get(f"/api/v1/scan/result/{jid}", params={"limit": 0}).status_code == 400
    assert client.get("/api/v1/jobs/jobs", params={"status": "bogus"}).status_code == 400


def test_api_key_enforced_when_configured(settings, store, service):
    from fastapi.testclient import TestClient
    from app.main import create_app
    settings.service_api_key = "secret"
    c = TestClient(create_app(settings, store, service))
    assert c.get("/api/v1/jobs/statistics").status_code == 401
    assert c.get("/api/v1/jobs/statistics", headers={"X-API-Key": "secret"}).status_code == 200
    assert c.get("/health").status_code == 200  # health stays open


def test_orphaned_jobs_failed_on_startup_and_resumable(settings, store, service, mock_hubspot):
    from fastapi.testclient import TestClient
    from app.main import create_app
    job = store.create("acme", "in_progress")
    c = TestClient(create_app(settings, store, service))
    st = c.get(f"/api/v1/scan/status/{job['job_id']}").json()
    assert st["status"] == "failed" and "restart" in st["error"]
    assert c.post(f"/api/v1/scan/resume/{job['job_id']}", json={"access_token": TOKEN}).status_code == 202
    assert c.get(f"/api/v1/scan/status/{job['job_id']}").json()["status"] == "completed"
