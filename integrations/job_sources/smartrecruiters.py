"""SmartRecruiters public Posting API adapter.

Uses SmartRecruiters' official, public, unauthenticated Posting API
(https://developers.smartrecruiters.com/docs/posting-api) that companies use to publish
their open positions. Read-only; paginated with offset/limit and filtered server-side by
country (India) so only relevant postings are fetched. The job description comes from the
posting detail endpoint, fetched only for postings the core decides to keep.

Timestamp: ``releasedDate`` - when the posting was released (posted_at_kind = "released").
Salary: this API does not publish pay, so salary stays UNKNOWN.
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
)
from database.enums import RemoteType
from integrations.job_sources.base import (
    JobCandidate,
    JobSource,
    RawJob,
    SearchQuery,
    SearchTask,
    SourceError,
    parse_datetime,
)
from integrations.job_sources.common import company_entries, location_text
from integrations.job_sources.http import PoliteHttp

API_BASE = "https://api.smartrecruiters.com/v1/companies"


class SmartRecruitersSource(JobSource):
    name = "smartrecruiters"
    discovery_method = (
        "SmartRecruiters Posting API: GET /v1/companies/{company}/postings?country=in "
        "(public, no auth), plus the posting detail for kept jobs"
    )
    docs_url = "https://developers.smartrecruiters.com/docs/posting-api"
    timestamp_kind = "released"
    server_side_search = True  # country filter
    paginated = True

    def __init__(
        self,
        companies: list[Any],
        *,
        skills_vocabulary: list[str],
        user_agent: str,
        country: str = "in",
        request_delay_seconds: float = 1.0,
        page_size: int = 100,
        max_pages: int = 5,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.companies = company_entries(companies)
        if not self.companies:
            raise ValueError("SmartRecruiters source needs at least one company")
        self.names = dict(self.companies)
        self.country = country
        self.skills_vocabulary = skills_vocabulary
        self.page_size = page_size
        self.max_pages = max_pages
        self.http = PoliteHttp(
            "SmartRecruiters", user_agent=user_agent, delay_seconds=request_delay_seconds,
            client=client, sleep=sleep,
        )  # fmt: skip

    def plan(self, query: SearchQuery) -> list[SearchTask]:
        return [
            SearchTask(
                label=f"SmartRecruiters company '{slug}' ({self.country})", params={"company": slug}
            )  # fmt: skip
            for slug, _ in self.companies
        ]

    def search_jobs(self, task: SearchTask, query: SearchQuery) -> list[RawJob]:
        slug = task.params["company"]
        out: list[RawJob] = []
        for page in range(self.max_pages):
            data = self.http.get_json(
                f"{API_BASE}/{slug}/postings",
                params={"country": self.country, "limit": self.page_size,
                        "offset": page * self.page_size},
            )  # fmt: skip
            if data is None:
                raise SourceError(f"SmartRecruiters company '{slug}' does not exist")
            content = data.get("content") if isinstance(data, dict) else None
            if not isinstance(content, list):
                raise SourceError(
                    f"SmartRecruiters company '{slug}' returned an unexpected payload"
                )
            out += [
                RawJob(source_job_id=f"{slug}:{job['id']}", payload={**job, "_company": slug})
                for job in content
                if isinstance(job, dict) and job.get("id")
            ]
            total = data.get("totalFound")
            if not content or not isinstance(total, int) or (page + 1) * self.page_size >= total:
                break
        return out

    def get_job(self, source_job_id: str) -> RawJob | None:
        slug, _, job_id = source_job_id.partition(":")
        if not slug or not job_id:
            raise ValueError(f"Malformed SmartRecruiters job id {source_job_id!r}")
        data = self.http.get_json(f"{API_BASE}/{slug}/postings/{job_id}")
        if not isinstance(data, dict):
            return None
        return RawJob(source_job_id=source_job_id, payload={**data, "_company": slug})

    def enrich(self, raw: RawJob) -> RawJob:
        detail = self.get_job(raw.source_job_id)
        return (
            RawJob(source_job_id=raw.source_job_id, payload={**raw.payload, **detail.payload})
            if detail
            else raw
        )

    def normalize_job(self, raw: RawJob) -> JobCandidate:
        p: dict[str, Any] = raw.payload
        slug = p.get("_company") or ""
        loc = p.get("location") or {}
        remote = bool(loc.get("remote"))
        location = location_text([loc.get("city"), loc.get("region")], loc.get("country"), remote)
        sections = ((p.get("jobAd") or {}).get("sections") or {}) if p.get("jobAd") else {}
        parts = [
            sections.get(k) or {}
            for k in ("jobDescription", "qualifications", "additionalInformation")
        ]
        html = "".join(f"<h3>{s.get('title') or ''}</h3>{s.get('text') or ''}" for s in parts if s)
        description, requirements = html_to_text(html or None)
        level = (p.get("experienceLevel") or {}).get("label")
        released = parse_datetime(p.get("releasedDate"))
        title = (p.get("name") or "").strip()
        company = (p.get("company") or {}).get("name") or self.names.get(slug, slug)
        url = p.get("postingUrl") or (f"https://jobs.smartrecruiters.com/{slug}/{p.get('id')}"
                                      if p.get("id") else "")  # fmt: skip
        return JobCandidate(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=company or None,
            title=title,
            location=location,
            remote_type=RemoteType.REMOTE if remote else detect_remote_type(location, title),
            experience_required=extract_experience(description)
            or (f"{level} (source label)" if level else None),
            description=description,
            requirements=requirements,
            skills=extract_skills(description, self.skills_vocabulary),
            url=url,
            application_url=p.get("applyUrl") or url or None,
            posted_at=released,
            posted_at_kind="released" if released else "unknown",
            raw={
                "id": p.get("id"),
                "_company": slug,
                "releasedDate": p.get("releasedDate"),
                "experienceLevel": p.get("experienceLevel"),
                "typeOfEmployment": p.get("typeOfEmployment"),
                "location": loc,
            },
        )

    def close(self) -> None:
        self.http.close()
