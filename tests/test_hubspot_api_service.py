import pytest
import responses

from services.hubspot_api_service import (HubSpotAPIError, HubSpotAPIService, HubSpotAuthError,
                                          HubSpotRateLimitError, RateLimiter)

BASE = "https://api.hubapi.com"
URL = f"{BASE}/crm/v3/objects/deals"


def svc(retries=2, sleeps=None):
    sl = (lambda s: sleeps.append(s)) if sleeps is not None else (lambda s: None)
    return HubSpotAPIService("tok-123456789", BASE, 5, retries, 1000, 1, sleep=sl)


def test_empty_token_rejected():
    with pytest.raises(HubSpotAuthError):
        HubSpotAPIService("  ")


@responses.activate
def test_pagination_follows_cursor():
    responses.add(responses.GET, URL, json={"results": [{"id": "1"}], "paging": {"next": {"after": "1"}}})
    responses.add(responses.GET, URL, json={"results": [{"id": "2"}]})
    pages = list(svc().get_deals())
    assert [p.results[0]["id"] for p in pages] == ["1", "2"]
    assert "after=1" in responses.calls[1].request.url
    assert responses.calls[0].request.headers["Authorization"] == "Bearer tok-123456789"


@responses.activate
@pytest.mark.parametrize("code", [401, 403])
def test_auth_errors_not_retried(code):
    responses.add(responses.GET, URL, status=code, json={"message": "nope"})
    with pytest.raises(HubSpotAuthError):
        svc().validate_credentials()
    assert len(responses.calls) == 1


@responses.activate
def test_429_retries_using_retry_after_then_succeeds():
    sleeps = []
    responses.add(responses.GET, URL, status=429, headers={"Retry-After": "3"}, json={})
    responses.add(responses.GET, URL, json={"results": []})
    assert svc(sleeps=sleeps).validate_credentials()
    assert 3.0 in sleeps


@responses.activate
def test_429_exhausts_retries():
    responses.add(responses.GET, URL, status=429, json={})
    with pytest.raises(HubSpotRateLimitError):
        svc(retries=1).get_deals_page()
    assert len(responses.calls) == 2


@responses.activate
def test_5xx_retried_then_raises():
    responses.add(responses.GET, URL, status=502, json={"message": "bad gateway"})
    with pytest.raises(HubSpotAPIError) as e:
        svc(retries=2).get_deals_page()
    assert e.value.retryable and len(responses.calls) == 3


@responses.activate
def test_400_not_retried():
    responses.add(responses.GET, URL, status=400, json={"message": "bad property"})
    with pytest.raises(HubSpotAPIError) as e:
        svc().get_deals_page()
    assert e.value.status_code == 400 and not e.value.retryable


def test_rate_limiter_blocks_when_window_full():
    t = {"now": 0.0}
    slept = []
    def sleep(s):
        slept.append(s); t["now"] += s
    rl = RateLimiter(2, 10, clock=lambda: t["now"], sleep=sleep)
    for _ in range(3):
        rl.acquire()
    assert slept and slept[0] == pytest.approx(10)
