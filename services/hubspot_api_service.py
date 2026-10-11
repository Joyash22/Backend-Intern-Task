"""HubSpot CRM v3 client for the deals object.

Features: private-app bearer auth, cursor pagination, sliding-window client-side
rate limiting (default 150 req / 10 s), retry with backoff on 429 / 5xx / network
errors, credential validation and structured logging. The token is never logged.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Iterator, List, Optional

import requests

logger = logging.getLogger(__name__)

DEALS_PATH = "/crm/v3/objects/deals"

DEFAULT_DEAL_PROPERTIES: List[str] = [
    "dealname", "amount", "dealstage", "pipeline", "dealtype", "description",
    "closedate", "createdate", "hs_lastmodifieddate", "hubspot_owner_id",
    "hs_is_closed_won", "hs_is_closed", "hs_priority",
]


class HubSpotAPIError(Exception):
    """Base error. `retryable` tells callers whether retrying could help."""

    def __init__(self, message: str, status_code: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


class HubSpotAuthError(HubSpotAPIError):
    """401 (bad/expired token) or 403 (missing crm.objects.deals.read scope)."""


class HubSpotRateLimitError(HubSpotAPIError):
    """429 that persisted after all retries."""


class RateLimiter:
    """Thread-safe sliding window limiter: at most `max_calls` per `period` seconds."""

    def __init__(self, max_calls: int, period: float,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.max_calls, self.period = max_calls, period
        self._clock, self._sleep = clock, sleep
        self._calls: Deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                wait = self.period - (now - self._calls[0])
            logger.debug("Client-side rate limit reached, sleeping %.2fs", wait)
            self._sleep(max(wait, 0.01))


@dataclass
class DealsPage:
    results: List[dict] = field(default_factory=list)
    next_after: Optional[str] = None


class HubSpotAPIService:
    def __init__(self, access_token: str, base_url: str = "https://api.hubapi.com",
                 timeout: int = 30, max_retries: int = 5,
                 rate_limit_requests: int = 150, rate_limit_window: float = 10,
                 session: Optional[requests.Session] = None,
                 sleep: Callable[[float], None] = time.sleep):
        if not access_token or not access_token.strip():
            raise HubSpotAuthError("HubSpot access token is empty", 401)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self._limiter = RateLimiter(rate_limit_requests, rate_limit_window, sleep=sleep)
        self._session = session or requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {access_token.strip()}",
            "Content-Type": "application/json",
        })

    # ------------------------------------------------------------------ core
    def _request(self, path: str, params: Optional[Dict] = None) -> dict:
        url = f"{self.base_url}{path}"
        last_error: Optional[HubSpotAPIError] = None
        for attempt in range(self.max_retries + 1):
            self._limiter.acquire()
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = HubSpotAPIError(f"Network error talking to HubSpot: {exc.__class__.__name__}",
                                             retryable=True)
                logger.warning("HubSpot network error (attempt %d/%d): %s",
                               attempt + 1, self.max_retries + 1, exc.__class__.__name__)
                self._backoff(attempt)
                continue

            status = resp.status_code
            if status == 200:
                return resp.json()
            if status in (401, 403):
                raise HubSpotAuthError(self._message(resp, "Authentication/authorization failed"), status)
            if status == 429:
                delay = self._retry_after(resp, attempt)
                last_error = HubSpotRateLimitError("HubSpot rate limit exceeded", 429, retryable=True)
                logger.warning("HubSpot 429 (attempt %d/%d), sleeping %.1fs",
                               attempt + 1, self.max_retries + 1, delay)
                self._sleep(delay)
                continue
            if 500 <= status < 600:
                last_error = HubSpotAPIError(self._message(resp, "HubSpot server error"), status, retryable=True)
                logger.warning("HubSpot %d (attempt %d/%d)", status, attempt + 1, self.max_retries + 1)
                self._backoff(attempt)
                continue
            # other 4xx: caller error, not retryable
            raise HubSpotAPIError(self._message(resp, f"HubSpot request failed ({status})"), status)
        assert last_error is not None
        raise last_error

    @staticmethod
    def _message(resp: requests.Response, default: str) -> str:
        try:
            return f"{default}: {resp.json().get('message', resp.text[:200])}"
        except ValueError:
            return f"{default}: {resp.text[:200]}"

    def _retry_after(self, resp: requests.Response, attempt: int) -> float:
        try:
            return max(float(resp.headers["Retry-After"]), 0.5)
        except (KeyError, ValueError):
            return min(2 ** attempt, 30)

    def _backoff(self, attempt: int) -> None:
        self._sleep(min(2 ** attempt, 30))

    # ------------------------------------------------------------------- API
    def validate_credentials(self) -> bool:
        """Cheap call (limit=1) proving the token is valid and has deals read scope."""
        self._request(DEALS_PATH, {"limit": 1})
        logger.info("HubSpot credentials validated")
        return True

    def get_deals_page(self, after: Optional[str] = None, properties: Optional[List[str]] = None,
                       limit: int = 100, archived: bool = False) -> DealsPage:
        params: Dict[str, object] = {
            "limit": max(1, min(limit, 100)),
            "properties": ",".join(properties or DEFAULT_DEAL_PROPERTIES),
            "archived": str(archived).lower(),
        }
        if after:
            params["after"] = after
        body = self._request(DEALS_PATH, params)
        next_after = (body.get("paging") or {}).get("next", {}).get("after")
        results = body.get("results", [])
        logger.info("Fetched %d deals (after=%s, next=%s)", len(results), after, next_after)
        return DealsPage(results=results, next_after=next_after)

    def get_deals(self, properties: Optional[List[str]] = None, limit: int = 100,
                  archived: bool = False, after: Optional[str] = None,
                  max_pages: Optional[int] = None) -> Iterator[DealsPage]:
        """Yield pages until HubSpot returns no `paging.next.after` cursor."""
        pages = 0
        while True:
            page = self.get_deals_page(after, properties, limit, archived)
            yield page
            pages += 1
            if not page.next_after or (max_pages and pages >= max_pages):
                return
            after = page.next_after
