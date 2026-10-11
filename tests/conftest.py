import pytest
import responses
from fastapi.testclient import TestClient

from app.config import Settings
from app.jobs import JobStore
from app.main import create_app
from services.extraction_service import ExtractionService
from services.hubspot_api_service import HubSpotAPIService

BASE = "https://api.hubapi.com"
TOKEN = "pat-na1-test-token-0000"

SAMPLE = [  # mirrors the 5 deals required by the task (amounts / stages)
    ("101", "Qualified Deal",      "5000",   "qualifiedtobuy",       "2026-11-01T00:00:00Z"),
    ("102", "Presentation Deal",   "25000",  "presentationscheduled", "2026-11-15T00:00:00Z"),
    ("103", "Closed Won Deal",     "50000",  "closedwon",            "2026-09-30T00:00:00Z"),
    ("104", "Closed Lost Deal",    "75000",  "closedlost",           "2026-09-15T00:00:00Z"),
    ("105", "Big Qualified Deal",  "100000", "qualifiedtobuy",       "2026-12-20T00:00:00Z"),
]


def hs_deal(i, name, amount, stage, close):
    return {"id": i, "archived": False, "createdAt": "2026-01-01T00:00:00Z",
            "properties": {"dealname": name, "amount": amount, "dealstage": stage, "pipeline": "default",
                           "closedate": close, "createdate": "2026-01-01T00:00:00.000Z",
                           "hs_lastmodifieddate": "2026-02-01T10:00:00.000Z", "dealtype": "newbusiness",
                           "description": f"{name} description", "hs_is_closed_won": str(stage == "closedwon").lower()}}


class InlineExecutor:
    """Runs submitted jobs synchronously so tests are deterministic."""
    def submit(self, fn, *a, **k):
        fn(*a, **k)


@pytest.fixture
def settings(tmp_path):
    return Settings(destination="duckdb", duckdb_path=str(tmp_path / "t.duckdb"),
                    pipelines_dir=str(tmp_path / "pipelines"), checkpoint_every_n_pages=1,
                    hubspot_page_size=2, hubspot_max_retries=0, service_api_key="")


@pytest.fixture
def store(settings):
    return JobStore(settings.jobs_database_url)


@pytest.fixture
def service(settings, store):
    def factory(token):
        return HubSpotAPIService(token, BASE, 5, settings.hubspot_max_retries, 1000, 1, sleep=lambda s: None)
    return ExtractionService(settings, store, api_factory=factory, executor=InlineExecutor())


@pytest.fixture
def client(settings, store, service):
    return TestClient(create_app(settings, store, service))


@pytest.fixture
def mock_hubspot():
    """Mocked HubSpot deals endpoint paginating 2 deals per page via `after`."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rs:
        def cb(request):
            import json
            after = int(request.params.get("after", 0))
            limit = int(request.params.get("limit", 100))
            chunk = SAMPLE[after:after + limit]
            body = {"results": [hs_deal(*d) for d in chunk]}
            if after + limit < len(SAMPLE):
                body["paging"] = {"next": {"after": str(after + limit)}}
            return 200, {}, json.dumps(body)
        rs.add_callback(responses.GET, f"{BASE}/crm/v3/objects/deals", callback=cb,
                        content_type="application/json")
        yield rs
