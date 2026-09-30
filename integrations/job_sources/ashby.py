"""Ashby public Job Board API adapter.

Uses Ashby's official, public, unauthenticated job-posting API
(https://developers.ashbyhq.com/docs/public-job-posting-api) that companies use to list
their open roles. Read-only; one request per board (the API returns every listed job).

Timestamp: ``publishedAt`` (posted_at_kind = "published").
Salary: only a single, explicit "Salary" component with a yearly or monthly interval is
normalized; the compensation summary text is always kept as the raw value.
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
    parse_datetime,
)
from integrations.job_sources.common import company_entries, location_text, number
from integrations.job_sources.http import PoliteHttp

API_BASE = "https://api.ashbyhq.com/posting-api/job-board"
_WORKPLACE = {"remote": RemoteType.REMOTE, "hybrid": RemoteType.HYBRID, "onsite": RemoteType.ONSITE}


class AshbySource(JobSource):
    name = "ashby"
    discovery_method = "Ashby Job Board API: GET /posting-api/job-board/{board} (public, no auth)"
    docs_url = "https://developers.ashbyhq.com/docs/public-job-posting-api"
    timestamp_kind = "published"

    def __init__(
        self,
        boards: list[Any],
        *,
        skills_vocabulary: list[str],
        user_agent: str,
        request_delay_seconds: float = 1.0,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.boards = company_entries(boards)
        if not self.boards:
            raise ValueError("Ashby source needs at least one board")
        self.names = dict(self.boards)
        self.skills_vocabulary = skills_vocabulary
        self.http = PoliteHttp(
            "Ashby", user_agent=user_agent, delay_seconds=request_delay_seconds, client=client,
            sleep=sleep,
        )  # fmt: skip

    def plan(self, query: SearchQuery) -> list[SearchTask]:
        return [
            SearchTask(label=f"Ashby board '{slug}'", params={"board": slug})
            for slug, _ in self.boards
        ]

    def search_jobs(self, task: SearchTask, query: SearchQuery) -> list[RawJob]:
        slug = task.params["board"]
        data = self.http.get_json(f"{API_BASE}/{slug}", params={"includeCompensation": "true"})
        if data is None:
            raise SourceError(f"Ashby board '{slug}' does not exist")
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise SourceError(f"Ashby board '{slug}' returned an unexpected payload")
        return [
            RawJob(source_job_id=f"{slug}:{job['id']}", payload={**job, "_board": slug})
            for job in jobs
            if isinstance(job, dict) and job.get("id") and job.get("isListed", True) is not False
        ]

    def get_job(self, source_job_id: str) -> RawJob | None:
        return None  # the board listing already carries every field

    def normalize_job(self, raw: RawJob) -> JobCandidate:
        p: dict[str, Any] = raw.payload
        slug = p.get("_board") or ""
        postal = ((p.get("address") or {}).get("postalAddress") or {}) if p.get("address") else {}
        workplace = (p.get("workplaceType") or "").lower()
        remote = bool(p.get("isRemote")) or workplace == "remote"
        places = [
            p.get("location"),
            *[(s or {}).get("location") for s in p.get("secondaryLocations") or []],
            postal.get("addressLocality"),
            postal.get("addressRegion"),
        ]
        location = location_text(places, postal.get("addressCountry"), remote)
        description, requirements = html_to_text(
            p.get("descriptionHtml") or p.get("descriptionPlain")
        )
        comp = p.get("compensation") if isinstance(p.get("compensation"), dict) else None
        low, high, currency, period = _salary(comp)
        title = (p.get("title") or "").strip()
        published = parse_datetime(p.get("publishedAt"))
        return JobCandidate(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=self.names.get(slug, slug) or None,
            title=title,
            location=location,
            remote_type=(RemoteType.REMOTE if remote else _WORKPLACE.get(workplace))
            or detect_remote_type(location, title),
            salary_min=low,
            salary_max=high,
            salary_currency=currency,
            salary_period=period,
            salary_raw=_raw_compensation(comp),
            experience_required=extract_experience(description),
            description=description,
            requirements=requirements,
            skills=extract_skills(description, self.skills_vocabulary),
            url=p.get("jobUrl") or "",
            application_url=p.get("applyUrl") or p.get("jobUrl"),
            posted_at=published,
            posted_at_kind="published" if published else "unknown",
            raw={k: p.get(k) for k in ("id", "_board", "publishedAt", "employmentType",
                                        "department", "team", "workplaceType", "isRemote")},
        )  # fmt: skip

    def close(self) -> None:
        self.http.close()


def _salary(
    comp: dict[str, Any] | None,
) -> tuple[float | None, float | None, str | None, str | None]:
    """Only one explicit Salary component is used; equity/bonus/multiple tiers are not."""
    if not comp:
        return None, None, None, None
    salaries = [
        c for c in comp.get("summaryComponents") or []
        if isinstance(c, dict) and (c.get("compensationType") or "").lower() == "salary"
    ]  # fmt: skip
    if len(salaries) != 1:
        return None, None, None, None
    c = salaries[0]
    return normalize_salary(
        number(c.get("minValue")), number(c.get("maxValue")), c.get("currencyCode"),
        c.get("interval"),
    )  # fmt: skip


def _raw_compensation(comp: dict[str, Any] | None) -> dict[str, Any] | None:
    """The compensation exactly as published, or None when nothing was disclosed."""
    if not comp:
        return None
    summary = comp.get("compensationTierSummary")
    components = comp.get("summaryComponents") or []
    if not summary and not components:
        return None
    return {"summary": summary, "components": components}
