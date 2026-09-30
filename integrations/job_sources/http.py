"""Polite, read-only HTTP for job-source adapters.

- identifies PIE with its User-Agent; waits between requests (per source)
- retries network errors, timeouts and 5xx a bounded number of times
- 429: stops querying the source for the rest of the run (SourceRateLimited)
- 401/403: SourceAuthRequired (never bypassed)
- 404: None
- an optional per-run request budget, for sources whose terms ask for low volume
"""

import time
from collections.abc import Callable
from typing import Any

import httpx2 as httpx

from integrations.job_sources.base import (
    SourceAuthRequired,
    SourceError,
    SourceRateLimited,
    SourceTimeout,
)

_RETRYABLE = {500, 502, 503, 504}


class PoliteHttp:
    def __init__(
        self,
        source: str,
        *,
        user_agent: str,
        delay_seconds: float = 1.0,
        timeout_seconds: float = 30.0,
        max_attempts: int = 3,
        max_requests: int | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        secrets: list[str] | None = None,
    ) -> None:
        self.source = source
        # Values (API keys, app ids) that must never appear in an error message or log.
        self._secrets = [s for s in secrets or [] if s and len(s) >= 4]
        self.delay = delay_seconds
        self.max_attempts = max_attempts
        self.max_requests = max_requests
        self.requests = 0
        self._sleep = sleep
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )
        self._last = 0.0
        self._rate_limited = False

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any | None:
        try:
            return self._get_json(url, params)
        except SourceAuthRequired as exc:
            raise SourceAuthRequired(self._scrub(str(exc)), self._scrub(exc.user_action)) from None
        except SourceError as exc:
            raise type(exc)(self._scrub(str(exc))) from None

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any | None:
        if self._rate_limited:
            raise SourceRateLimited(f"{self.source} rate limit hit earlier in this run")
        for attempt in range(1, self.max_attempts + 1):
            if self.max_requests is not None and self.requests >= self.max_requests:
                raise SourceRateLimited(
                    f"{self.source}: per-run request budget ({self.max_requests}) used up"
                )
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            try:
                response = self._client.get(url, params=params)
            except httpx.TimeoutException as exc:
                if attempt == self.max_attempts:
                    raise SourceTimeout(f"{self.source} timed out: {url}") from exc
                self._sleep(2**attempt)
                continue
            except httpx.TransportError as exc:
                if attempt == self.max_attempts:
                    raise SourceError(f"Network error calling {self.source}: {exc}") from exc
                self._sleep(2**attempt)
                continue
            status = response.status_code
            if status == 404:
                return None
            if status in (401, 403):
                raise SourceAuthRequired(
                    f"{self.source} returned HTTP {status} for {url}",
                    user_action="This board is not public; remove it from config/job_sources.yaml.",
                )
            if status == 429:
                self._rate_limited = True  # respect it: no more requests this run
                raise SourceRateLimited(f"{self.source} asked us to slow down (HTTP 429)")
            if status in _RETRYABLE and attempt < self.max_attempts:
                self._sleep(2**attempt)
                continue
            if status >= 400:
                raise SourceError(f"{self.source} returned HTTP {status} for {url}")
            try:
                return response.json()
            except ValueError as exc:
                raise SourceError(f"{self.source} returned invalid JSON for {url}") from exc
        raise SourceError(f"{self.source} request failed after retries")  # pragma: no cover

    def close(self) -> None:
        self._client.close()
