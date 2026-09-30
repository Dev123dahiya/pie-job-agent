"""Greenhouse Job Board API adapter.

Uses the official, public, unauthenticated Job Board API
(https://developers.greenhouse.io/job-board.html) that companies publish so their postings
can be embedded elsewhere. Read-only; one list request per board per run, plus an optional
detail request per NEW matching posting to read published pay ranges.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx2 as httpx

from backend.core.normalize import (
    detect_remote_type,
    extract_experience,
    extract_skills,
    html_to_text,
)
from integrations.job_sources.base import (
    JobCandidate,
    JobSource,
    RawJob,
    SearchQuery,
    SearchTask,
    SourceAuthRequired,
    SourceError,
    SourceRateLimited,
    parse_datetime,
)

logger = logging.getLogger(__name__)

API_BASE = "https://boards-api.greenhouse.io/v1/boards"
_RETRYABLE = {500, 502, 503, 504}


class GreenhouseSource(JobSource):
    name = "greenhouse"
    discovery_method = "Greenhouse Job Board API: GET /v1/boards/{board}/jobs (public, no auth)"
    docs_url = "https://developers.greenhouse.io/job-board.html"
    timestamp_kind = "published"

    def __init__(
        self,
        boards: list[str],
        *,
        skills_vocabulary: list[str],
        user_agent: str,
        request_delay_seconds: float = 1.0,
        fetch_pay_ranges: bool = True,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
    ) -> None:
        if not boards:
            raise ValueError("Greenhouse source needs at least one board token")
        self.boards = boards
        self.skills_vocabulary = skills_vocabulary
        self.delay = request_delay_seconds
        self.fetch_pay_ranges = fetch_pay_ranges
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._client = client or httpx.Client(
            timeout=30.0, headers={"User-Agent": user_agent, "Accept": "application/json"}
        )
        self._last_request = 0.0
        self._rate_limited = False

    # --- HTTP ---------------------------------------------------------------------------

    def _get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response | None:
        """GET with politeness delay and bounded retries. Returns None on 404."""
        if self._rate_limited:
            raise SourceRateLimited("Greenhouse rate limit hit earlier in this run")
        for attempt in range(1, self.max_attempts + 1):
            wait = self.delay - (time.monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)
            self._last_request = time.monotonic()
            try:
                response = self._client.get(url, params=params)
            except httpx.TransportError as exc:
                if attempt == self.max_attempts:
                    raise SourceError(f"Network error calling Greenhouse: {exc}") from exc
                self._sleep(2**attempt)
                continue
            if response.status_code == 404:
                return None
            if response.status_code in (401, 403):
                raise SourceAuthRequired(
                    f"Greenhouse returned {response.status_code} for {url}",
                    user_action="This board is not public; remove it from config/job_sources.yaml.",
                )
            if response.status_code == 429:
                # Respect the limit: stop querying this source for the rest of the run.
                self._rate_limited = True
                raise SourceRateLimited("Greenhouse asked us to slow down (HTTP 429)")
            if response.status_code in _RETRYABLE and attempt < self.max_attempts:
                self._sleep(2**attempt)
                continue
            if response.status_code >= 400:
                raise SourceError(f"Greenhouse returned HTTP {response.status_code} for {url}")
            return response
        raise SourceError("Greenhouse request failed after retries")  # pragma: no cover

    # --- JobSource ----------------------------------------------------------------------

    def plan(self, query: SearchQuery) -> list[SearchTask]:
        # The board API has no server-side search, so one task per board; the core filters.
        return [
            SearchTask(label=f"Greenhouse board '{b}'", params={"board": b}) for b in self.boards
        ]

    def search_jobs(self, task: SearchTask, query: SearchQuery) -> list[RawJob]:
        board = task.params["board"]
        response = self._get(f"{API_BASE}/{board}/jobs", params={"content": "true"})
        if response is None:
            raise SourceError(f"Greenhouse board '{board}' does not exist")
        try:
            jobs = response.json().get("jobs", [])
        except ValueError as exc:
            raise SourceError(f"Greenhouse board '{board}' returned invalid JSON") from exc
        return [
            RawJob(source_job_id=f"{board}:{job['id']}", payload={**job, "_board": board})
            for job in jobs
            if isinstance(job, dict) and job.get("id") is not None
        ]

    def get_job(self, source_job_id: str) -> RawJob | None:
        board, _, job_id = source_job_id.partition(":")
        if not board or not job_id:
            raise ValueError(f"Malformed Greenhouse job id {source_job_id!r}")
        response = self._get(
            f"{API_BASE}/{board}/jobs/{job_id}", params={"pay_transparency": "true"}
        )
        if response is None:
            return None
        return RawJob(source_job_id=source_job_id, payload={**response.json(), "_board": board})

    def enrich(self, raw: RawJob) -> RawJob:
        if not self.fetch_pay_ranges:
            return raw
        detailed = self.get_job(raw.source_job_id)
        return detailed or raw

    def normalize_job(self, raw: RawJob) -> JobCandidate:
        p: dict[str, Any] = raw.payload
        title = (p.get("title") or "").strip()
        url = p.get("absolute_url") or ""
        location = ((p.get("location") or {}).get("name") or "").strip() or None
        description, requirements = html_to_text(p.get("content"))
        salary_min, salary_max, currency = _pay_range(p.get("pay_input_ranges"))
        metadata_text = " ".join(
            str(m.get("value")) for m in p.get("metadata") or [] if isinstance(m, dict)
        )
        return JobCandidate(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=(p.get("company_name") or "").strip() or p.get("_board"),
            title=title,
            location=location,
            remote_type=detect_remote_type(location, title, metadata_text),
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            salary_period=None,  # Greenhouse pay ranges do not state an interval
            salary_raw={"pay_input_ranges": p.get("pay_input_ranges")}
            if p.get("pay_input_ranges")
            else None,
            experience_required=extract_experience(description),
            description=description,
            requirements=requirements,
            skills=extract_skills(description, self.skills_vocabulary),
            url=url,
            # Greenhouse hosts the application form on the posting page itself.
            application_url=url or None,
            company_url=None,
            # first_published is when the posting went live; updated_at is NOT a post date.
            posted_at=parse_datetime(p.get("first_published")),
            posted_at_kind="published" if parse_datetime(p.get("first_published")) else "unknown",
            source_updated_at=parse_datetime(p.get("updated_at")),
            raw={
                k: p.get(k)
                for k in (
                    "id",
                    "internal_job_id",
                    "requisition_id",
                    "_board",
                    "first_published",
                    "updated_at",
                    "departments",
                    "offices",
                )
            },
        )

    def close(self) -> None:
        self._client.close()


def _pay_range(ranges: Any) -> tuple[float | None, float | None, str | None]:
    """Greenhouse pay ranges are in cents. Only a single, unambiguous range is used."""
    if not isinstance(ranges, list) or len(ranges) != 1 or not isinstance(ranges[0], dict):
        return None, None, None
    r = ranges[0]
    try:
        low = int(r["min_cents"]) / 100 if r.get("min_cents") is not None else None
        high = int(r["max_cents"]) / 100 if r.get("max_cents") is not None else None
    except (TypeError, ValueError):
        return None, None, None
    currency = r.get("currency_type") if (low or high) else None
    return low, high, currency
