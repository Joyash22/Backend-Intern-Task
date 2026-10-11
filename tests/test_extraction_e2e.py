"""End-to-end through the REST API + dlt + DuckDB, with HubSpot MOCKED (not a live HubSpot run)."""
import responses

from tests.conftest import BASE, SAMPLE, TOKEN

URL = f"{BASE}/crm/v3/objects/deals"


def test_full_extraction_five_deals(client, mock_hubspot):
    r = client.post("/api/v1/scan/start", json={"access_token": TOKEN, "tenant_id": "acme"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    st = client.get(f"/api/v1/scan/status/{job_id}").json()
    assert st["status"] == "completed" and st["record_count"] == 5
    assert st["pages_done"] == 3  # 2 + 2 + 1 with page size 2

    res = client.get(f"/api/v1/scan/result/{job_id}").json()
    assert res["total"] == 5 and len(res["records"]) == 5
    by_id = {x["id"]: x for x in res["records"]}
    assert set(by_id) == {d[0] for d in SAMPLE}
    assert by_id["104"]["dealstage"] == "closedlost" and by_id["105"]["amount"] == "100000.00"
    assert all(x["_tenant_id"] == "acme" and x["_scan_id"] == job_id for x in res["records"])

    # pagination
    p = client.get(f"/api/v1/scan/result/{job_id}", params={"limit": 2, "offset": 4}).json()
    assert len(p["records"]) == 1 and p["next_offset"] is None
    assert client.get(f"/api/v1/scan/result/{job_id}", params={"limit": 2}).json()["next_offset"] == 2


def test_checkpoint_and_resume_after_failure(client, settings, mock_hubspot):
    """Page 2 fails once (HTTP 503, no retries) -> job fails with checkpoint after page 1 -> resume completes."""
    import json
    state = {"fail": True}

    def cb(request):
        after = int(request.params.get("after", 0))
        if after == 2 and state["fail"]:
            return 503, {}, json.dumps({"message": "boom"})
        chunk = SAMPLE[after:after + 2]
        from tests.conftest import hs_deal
        body = {"results": [hs_deal(*d) for d in chunk]}
        if after + 2 < len(SAMPLE):
            body["paging"] = {"next": {"after": str(after + 2)}}
        return 200, {}, json.dumps(body)

    mock_hubspot.reset()
    mock_hubspot.add_callback(responses.GET, URL, callback=cb, content_type="application/json")

    job_id = client.post("/api/v1/scan/start", json={"access_token": TOKEN}).json()["job_id"]
    st = client.get(f"/api/v1/scan/status/{job_id}").json()
    assert st["status"] == "failed" and "server error" in st["error"].lower()
    assert st["checkpoint_after"] == "2" and st["record_count"] == 2

    state["fail"] = False
    assert client.post(f"/api/v1/scan/resume/{job_id}", json={"access_token": TOKEN}).status_code == 202
    st = client.get(f"/api/v1/scan/status/{job_id}").json()
    assert st["status"] == "completed" and st["record_count"] == 5
    assert client.get(f"/api/v1/scan/result/{job_id}").json()["total"] == 5  # no duplicates after resume


def test_rescan_is_idempotent(client, mock_hubspot):
    a = client.post("/api/v1/scan/start", json={"access_token": TOKEN}).json()["job_id"]
    b = client.post("/api/v1/scan/start", json={"access_token": TOKEN}).json()["job_id"]
    assert client.get(f"/api/v1/scan/result/{b}").json()["total"] == 5
    assert client.get(f"/api/v1/scan/result/{a}").json()["total"] == 0  # ownership moved by merge


def test_cleanup_removes_data(client, mock_hubspot):
    job_id = client.post("/api/v1/scan/start", json={"access_token": TOKEN}).json()["job_id"]
    assert client.delete(f"/api/v1/scan/remove/{job_id}").status_code == 200
    assert client.get(f"/api/v1/scan/status/{job_id}").status_code == 404
    assert client.get(f"/api/v1/scan/result/{job_id}").status_code == 404


def test_hubspot_unauthorized_marks_job_failed(client):
    with responses.RequestsMock() as rs:
        rs.add(responses.GET, URL, status=401, json={"message": "invalid token"})
        job_id = client.post("/api/v1/scan/start", json={"access_token": TOKEN}).json()["job_id"]
    st = client.get(f"/api/v1/scan/status/{job_id}").json()
    assert st["status"] == "failed" and "Authentication" in st["error"]
    assert client.get(f"/api/v1/scan/result/{job_id}").status_code == 409
