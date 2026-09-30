"""Remotive public remote-jobs API adapter.

Uses Remotive's official, public, documented API (https://remotive.com/api/remote-jobs,
docs: https://github.com/remotive-com/remote-jobs-api). Its terms ask for low volume and a
link back to Remotive as the source: PIE makes at most ``max_requests`` requests per run
(default 3) and keeps the Remotive URL on every job.

Timestamp: ``publication_date`` is published WITHOUT a timezone, so PIE stores the earliest
instant it could be (posted_at_kind = "published_tz_unknown"): a job can look older than it
is, never fresher. Salary is free text; it is kept as the raw value and never parsed into
numbers (formats like "$50k-70k" or "Competitive" are not reliably comparable).
"""

import time
from collections.abc import Callable
from typing import Any

import httpx2 as httpx

from backend.core.normalize import extract_experience, extract_skills, html_to_text
from database.enums import RemoteType
from integrations.job_sources.base import (
    JobCandidate,
    JobSource,
    RawJob,
    SearchQuery,
    SearchTask,
    SourceError,
    parse_naive_as_earliest,
)
from integrations.job_sources.http import PoliteHttp

API_URL = "https://remotive.com/api/remote-jobs"


class RemotiveSource(JobSource):
    name = "remotive"
    discovery_method = "Remotive API: GET /api/remote-jobs?category=... (public, no auth)"
    docs_url = "https://github.com/remotive-com/remote-jobs-api"
    timestamp_kind = "published_tz_unknown"
    server_side_search = True  # category filter

    def __init__(
        self,
        categories: list[str],
        *,
        skills_vocabulary: list[str],
        user_agent: str,
        request_delay_seconds: float = 2.0,
        max_requests: int = 3,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not categories:
            raise ValueError("Remotive source needs at least one category")
        self.categories = categories
        self.skills_vocabulary = skills_vocabulary
        self.http = PoliteHttp(
            "Remotive", user_agent=user_agent, delay_seconds=request_delay_seconds,
            max_requests=max_requests, client=client, sleep=sleep, max_attempts=1,
        )  # fmt: skip

    def plan(self, query: SearchQuery) -> list[SearchTask]:
        return [
            SearchTask(label=f"Remotive category '{c}'", params={"category": c})
            for c in self.categories
        ]

    def search_jobs(self, task: SearchTask, query: SearchQuery) -> list[RawJob]:
        data = self.http.get_json(API_URL, params={"category": task.params["category"]})
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise SourceError("Remotive returned an unexpected payload")
        return [
            RawJob(source_job_id=str(job["id"]), payload=job)
            for job in jobs
            if isinstance(job, dict) and job.get("id") is not None
        ]

    def get_job(self, source_job_id: str) -> RawJob | None:
        return None  # the listing carries every field; no per-job endpoint is used

    def normalize_job(self, raw: RawJob) -> JobCandidate:
        p: dict[str, Any] = raw.payload
        region = (p.get("candidate_required_location") or "").strip()
        location = f"Remote - {region}" if region else "Remote"
        description, requirements = html_to_text(p.get("description"))
        posted = parse_naive_as_earliest(p.get("publication_date"))
        salary_text = (p.get("salary") or "").strip()
        return JobCandidate(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=(p.get("company_name") or "").strip() or None,
            title=(p.get("title") or "").strip(),
            location=location,
            remote_type=RemoteType.REMOTE,
            salary_raw={"text": salary_text} if salary_text else None,
            experience_required=extract_experience(description),
            description=description,
            requirements=requirements,
            skills=extract_skills(description, self.skills_vocabulary),
            url=p.get("url") or "",
            application_url=p.get("url") or None,
            posted_at=posted,
            posted_at_kind="published_tz_unknown" if posted else "unknown",
            raw={k: p.get(k) for k in ("id", "category", "job_type", "publication_date",
                                        "candidate_required_location", "salary")},
        )  # fmt: skip

    def close(self) -> None:
        self.http.close()
