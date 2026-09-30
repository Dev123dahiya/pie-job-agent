"""Lever Postings API adapter.

Uses Lever's official, public, unauthenticated Postings API
(https://github.com/lever/postings-api), which companies publish so their open postings can
be listed on their own careers pages. Read-only; paginated with skip/limit.

Timestamp: Lever exposes only ``createdAt`` (when the posting was created). A posting is
published at or after creation, so using it never makes a job look newer than it is
(posted_at_kind = "created").
"""

import time
from collections.abc import Callable
from typing import Any

import httpx2 as httpx

from backend.core.normalize import (
    detect_remote_type,
    extract_experience,
    extract_skills,
    html_to_text,
    normalize_salary,
)
from database.enums import RemoteType
from integrations.job_sources.base import (
    JobCandidate,
    JobSource,
    RawJob,
    SearchQuery,
    SearchTask,
    SourceError,
    parse_epoch_millis,
)
from integrations.job_sources.common import company_entries, location_text, number
from integrations.job_sources.http import PoliteHttp

API_BASE = "https://api.lever.co/v0/postings"
_WORKPLACE = {"remote": RemoteType.REMOTE, "hybrid": RemoteType.HYBRID, "onsite": RemoteType.ONSITE}


class LeverSource(JobSource):
    name = "lever"
    discovery_method = "Lever Postings API: GET /v0/postings/{company}?mode=json (public, no auth)"
    docs_url = "https://github.com/lever/postings-api"
    timestamp_kind = "created"
    paginated = True

    def __init__(
        self,
        companies: list[Any],
        *,
        skills_vocabulary: list[str],
        user_agent: str,
        request_delay_seconds: float = 1.0,
        page_size: int = 100,
        max_pages: int = 10,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.companies = company_entries(companies)
        if not self.companies:
            raise ValueError("Lever source needs at least one company")
        self.names = dict(self.companies)
        self.skills_vocabulary = skills_vocabulary
        self.page_size = page_size
        self.max_pages = max_pages
        self.http = PoliteHttp(
            "Lever", user_agent=user_agent, delay_seconds=request_delay_seconds, client=client,
            sleep=sleep,
        )  # fmt: skip

    def plan(self, query: SearchQuery) -> list[SearchTask]:
        return [
            SearchTask(label=f"Lever company '{slug}'", params={"company": slug})
            for slug, _ in self.companies
        ]

    def search_jobs(self, task: SearchTask, query: SearchQuery) -> list[RawJob]:
        slug = task.params["company"]
        out: list[RawJob] = []
        for page in range(self.max_pages):
            data = self.http.get_json(
                f"{API_BASE}/{slug}",
                params={"mode": "json", "skip": page * self.page_size, "limit": self.page_size},
            )
            if data is None:
                raise SourceError(f"Lever company '{slug}' does not exist")
            if not isinstance(data, list):
                raise SourceError(f"Lever company '{slug}' returned an unexpected payload")
            out += [
                RawJob(source_job_id=f"{slug}:{job['id']}", payload={**job, "_company": slug})
                for job in data
                if isinstance(job, dict) and job.get("id")
            ]
            if len(data) < self.page_size:
                break
        return out

    def get_job(self, source_job_id: str) -> RawJob | None:
        slug, _, job_id = source_job_id.partition(":")
        if not slug or not job_id:
            raise ValueError(f"Malformed Lever job id {source_job_id!r}")
        data = self.http.get_json(f"{API_BASE}/{slug}/{job_id}", params={"mode": "json"})
        if not isinstance(data, dict):
            return None
        return RawJob(source_job_id=source_job_id, payload={**data, "_company": slug})

    def normalize_job(self, raw: RawJob) -> JobCandidate:
        p: dict[str, Any] = raw.payload
        slug = p.get("_company") or ""
        cats = p.get("categories") or {}
        workplace = (p.get("workplaceType") or "").lower()
        places = [cats.get("location"), *(cats.get("allLocations") or [])]
        location = location_text(places, p.get("country"), workplace == "remote")
        html = (
            (p.get("description") or "")
            + "".join(
                f"<h3>{item.get('text', '')}</h3>{item.get('content', '')}"
                for item in p.get("lists") or []
                if isinstance(item, dict)
            )
            + (p.get("additional") or "")
        )
        description, requirements = html_to_text(html or p.get("descriptionPlain"))
        salary = p.get("salaryRange") if isinstance(p.get("salaryRange"), dict) else None
        low, high, currency, period = normalize_salary(
            number((salary or {}).get("min")),
            number((salary or {}).get("max")),
            (salary or {}).get("currency"),
            (salary or {}).get("interval"),
        )
        title = (p.get("text") or "").strip()
        return JobCandidate(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=self.names.get(slug, slug) or None,
            title=title,
            location=location,
            remote_type=_WORKPLACE.get(workplace) or detect_remote_type(location, title),
            salary_min=low,
            salary_max=high,
            salary_currency=currency,
            salary_period=period,
            salary_raw=salary,
            experience_required=extract_experience(description),
            description=description,
            requirements=requirements,
            skills=extract_skills(description, self.skills_vocabulary),
            url=p.get("hostedUrl") or "",
            application_url=p.get("applyUrl") or p.get("hostedUrl"),
            posted_at=parse_epoch_millis(p.get("createdAt")),
            posted_at_kind="created" if parse_epoch_millis(p.get("createdAt")) else "unknown",
            raw={
                "id": p.get("id"),
                "_company": slug,
                "createdAt": p.get("createdAt"),
                "categories": cats,
                "country": p.get("country"),
                "workplaceType": p.get("workplaceType"),
            },
        )

    def close(self) -> None:
        self.http.close()
