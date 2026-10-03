"""Naukri and LinkedIn as job sources: search results read in a visible browser that uses the
user's own saved login (data/browser/<platform>/, see backend/scripts/login.py).

Read-only and low volume: a few searches per run (``max_searches``), one page each by
default, a pause between pages, and job pages opened only for postings PIE keeps
(``max_details``). If the platform shows a login page, the source stops and asks the user to
log in again (the platform is paused, REQUIREMENTS 4). If it refuses the browser, the source
stops and says so; PIE never disguises itself.

Post dates: the platforms show ages like "2 days ago". PIE stores the OLDEST time that can
mean ("2 days ago" -> 3 days before now), so a job never looks fresher than it is
(posted_at_kind = "relative_upper_bound").
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from backend.core.normalize import extract_experience, extract_skills
from integrations.job_sources.base import (
    JobCandidate,
    JobSource,
    RawJob,
    SearchQuery,
    SearchTask,
    SourceAuthRequired,
    SourceError,
    parse_naive_as_earliest,
)
from integrations.platforms.base import classify_page, parse_relative_age, save_status
from integrations.platforms.linkedin import selectors as linkedin
from integrations.platforms.naukri import selectors as naukri


class PlatformSearchSource(JobSource):
    requires_auth = True
    server_side_search = True
    paginated = True
    timestamp_kind = "relative_upper_bound"
    platform = ""  # naukri | linkedin
    selectors: Any = None
    render_all = False

    def __init__(
        self,
        *,
        browser: Any,
        skills_vocabulary: list[str],
        data_dir: Any,
        max_searches: int = 4,
        pages: int = 1,
        max_details: int = 15,
        request_delay_seconds: float = 8.0,
        experience_years: Callable[[], float | None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.browser = browser
        self.skills_vocabulary = skills_vocabulary
        self.data_dir = data_dir
        self.max_searches = max(1, max_searches)
        self.pages = max(1, min(pages, 3))
        self.max_details = max(0, max_details)
        self.delay = request_delay_seconds
        self.experience_years = experience_years or (lambda: None)
        self.sleep = sleep
        self.now = now
        self._requests = 0
        self._details = 0

    # --- search ------------------------------------------------------------------------

    def plan(self, query: SearchQuery) -> list[SearchTask]:
        pairs = [(t, loc) for t in query.terms for loc in query.locations][: self.max_searches]
        return [
            SearchTask(
                label=f"{self.selectors.NAME}: {t} in {loc} (page {p})",
                params={"term": t, "location": loc, "page": p},
            )  # fmt: skip
            for t, loc in pairs
            for p in range(1, self.pages + 1)
        ]

    def page_url(self, task: SearchTask, query: SearchQuery) -> str:
        raise NotImplementedError

    def _open(self, read: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        if self._requests:
            self.sleep(self.delay)
        self._requests += 1
        try:
            result = read()
        except Exception as exc:  # browser trouble is a source error, not a crash
            raise SourceError(f"{self.selectors.NAME}: the browser failed: {exc}") from exc
        check = classify_page(
            self.selectors, str(result.get("url") or ""), result.get("status"),
            str(result.get("text") or ""),
        )  # fmt: skip
        if check.state == "blocked":
            save_status(self.data_dir, self.platform, check)
            raise SourceError(f"{check.detail} PIE will not try to get around it.")
        if check.state == "expired" and any(
            part in str(result.get("url") or "").lower()
            for part in self.selectors.LOGGED_OUT_URL_PARTS
        ):
            save_status(self.data_dir, self.platform, check)
            raise SourceAuthRequired(
                f"{self.selectors.NAME} session expired",
                f"Log in again: python -m backend.scripts.login {self.platform}",
            )
        return result

    def search_jobs(self, task: SearchTask, query: SearchQuery) -> list[RawJob]:
        url = self.page_url(task, query)
        result = self._open(
            lambda: self.browser.read_cards(
                url, self.selectors.CARD_SPEC, render_all=self.render_all
            )
        )
        seen_at = self.now().isoformat()
        return [
            RawJob(source_job_id=str(item["id"]), payload={**item, "seen_at": seen_at})
            for item in result.get("items") or []
            if item.get("id") and item.get("title")
        ]

    def get_job(self, source_job_id: str) -> RawJob | None:
        return None  # postings are read from search results; details via enrich

    def enrich(self, raw: RawJob) -> RawJob:
        """Open the job's own page for the full description (capped per run)."""
        if self._details >= self.max_details:
            return raw
        self._details += 1
        url = self.job_url(raw)
        page = self._open(lambda: self.browser.read_page_text(url))
        return RawJob(
            source_job_id=raw.source_job_id,
            payload={**raw.payload, "description": page.get("text") or ""},
        )

    def job_url(self, raw: RawJob) -> str:
        raise NotImplementedError

    # --- normalize ---------------------------------------------------------------------

    def _posted(self, p: dict[str, Any]) -> tuple[datetime | None, str]:
        seen = datetime.fromisoformat(p["seen_at"]) if p.get("seen_at") else self.now()
        posted = parse_relative_age(p.get("posted") or "", seen)
        return (posted, "relative_upper_bound") if posted else (None, "unknown")

    def normalize_job(self, raw: RawJob) -> JobCandidate:
        p = raw.payload
        title = (p.get("title") or "").strip()
        if not title:
            raise ValueError("no title")
        description = p.get("description") or "\n".join(
            x for x in (p.get("snippet"), p.get("tags")) if x
        )
        posted_at, kind = self._posted(p)
        experience = (p.get("experience") or "").strip() or extract_experience(description)
        return JobCandidate(
            source=self.name,
            source_job_id=raw.source_job_id,
            company=(p.get("company") or "").strip() or None,
            title=title,
            location=(p.get("location") or "").strip() or None,
            experience_required=experience or None,
            description=description or None,
            skills=extract_skills(f"{description}\n{p.get('tags') or ''}", self.skills_vocabulary),
            url=self.job_url(raw),
            application_url=self.job_url(raw),
            posted_at=posted_at,
            posted_at_kind=kind,
            raw={
                "salary_text": p.get("salary"),
                "posted_text": p.get("posted"),
                "platform": self.platform,
            },  # fmt: skip
        )


class NaukriSource(PlatformSearchSource):
    name = "naukri"
    platform = "naukri"
    selectors = naukri
    discovery_method = "Naukri search results, read in a visible browser with your own login"
    docs_url = "https://www.naukri.com"

    def page_url(self, task: SearchTask, query: SearchQuery) -> str:
        years = self.experience_years()
        days = -(-query.max_age_hours // 24) if query.max_age_hours else None  # ceil
        return naukri.search_page_url(
            task.params["term"], task.params["location"], task.params["page"],
            int(years) if years is not None else None, days,
        )  # fmt: skip

    def job_url(self, raw: RawJob) -> str:
        url = str(raw.payload.get("url") or "")
        if not url.startswith("https://www.naukri.com/"):
            raise ValueError("Naukri job link missing or not on naukri.com")
        return url.split("?")[0]


class LinkedInSource(PlatformSearchSource):
    name = "linkedin"
    platform = "linkedin"
    selectors = linkedin
    render_all = True
    discovery_method = (
        "LinkedIn Easy Apply search results, read in a visible browser with your login"
    )
    docs_url = "https://www.linkedin.com/jobs"

    def page_url(self, task: SearchTask, query: SearchQuery) -> str:
        return linkedin.search_page_url(
            task.params["term"], task.params["location"], task.params["page"],
            query.max_age_hours, self.experience_years(),
        )  # fmt: skip

    def job_url(self, raw: RawJob) -> str:
        if not raw.source_job_id.isdigit():
            raise ValueError("LinkedIn job id is not numeric")
        return linkedin.job_url(raw.source_job_id)

    def normalize_job(self, raw: RawJob) -> JobCandidate:
        title = str(raw.payload.get("title") or "")
        for noise in linkedin.TITLE_NOISE:
            if title.lower().endswith(noise):
                title = title[: -len(noise)]
        return super().normalize_job(
            RawJob(source_job_id=raw.source_job_id, payload={**raw.payload, "title": title})
        )

    def _posted(self, p: dict[str, Any]) -> tuple[datetime | None, str]:
        if p.get("posted"):  # <time datetime="2026-10-02">: a date without a time zone
            posted = parse_naive_as_earliest(f"{p['posted'][:10]}T00:00:00")
            if posted:
                return posted, "published_tz_unknown"
        seen = datetime.fromisoformat(p["seen_at"]) if p.get("seen_at") else self.now()
        posted = parse_relative_age(p.get("footer") or "", seen)
        return (posted, "relative_upper_bound") if posted else (None, "unknown")
