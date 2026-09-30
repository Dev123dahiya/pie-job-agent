"""Adzuna Jobs API adapter (India).

Official API: https://developer.adzuna.com/ (``GET /v1/api/jobs/{country}/search/{page}``).
Credentials: the user's own free app_id/app_key from ADZUNA_APP_ID / ADZUNA_APP_KEY in .env.
They are sent only as query parameters and scrubbed from every error message; the stored job
URL is Adzuna's public details page, never the tracking redirect (which can embed the app id).

Usage terms (developer.adzuna.com/docs/terms_of_service): 25 requests/minute, 250/day. PIE
waits ``request_delay_seconds`` (default 3s) between requests and stops after
``max_requests`` per run.

What the fields mean - and what is NOT assumed:
- ``created``: Adzuna's documentation does not say whether this is the employer's posting
  date or when Adzuna indexed the advert. It is therefore NOT used as a post date by default:
  posted_at stays None (freshness UNKNOWN) and the value is kept in raw["adzuna_created"].
  ``created_is_publication: true`` in config switches it on - only do that if you have
  verified the meaning yourself.
- ``max_days_old`` (server-side) filters on Adzuna's own date. It is used only to avoid
  fetching obviously old adverts; it never makes a job fresh.
- ``salary_min``/``salary_max``: the documentation does not state a pay period, and
  ``salary_is_predicted`` marks Adzuna's own estimates. Numeric figures without a stated
  period stay UNKNOWN; estimates stay UNKNOWN. Only an explicit text figure with a period
  (e.g. "₹8,00,000/year", "₹80,000/month") is normalized. The original values are always
  kept in salary_raw.
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
    parse_salary_text,
)
from integrations.job_sources.base import (
    JobCandidate,
    JobSource,
    RawJob,
    SearchQuery,
    SearchTask,
    SourceError,
    parse_datetime,
    parse_naive_as_earliest,
)
from integrations.job_sources.http import PoliteHttp

API_BASE = "https://api.adzuna.com/v1/api/jobs"
_SITE = {"in": "https://www.adzuna.in", "gb": "https://www.adzuna.co.uk"}


class AdzunaSource(JobSource):
    name = "adzuna"
    requires_auth = True  # the user's own API key (not a login)
    discovery_method = (
        "Adzuna Jobs API: GET /v1/api/jobs/in/search/{page}?what=&where=&max_days_old= "
        "(official API, user's own free key)"
    )
    docs_url = "https://developer.adzuna.com/"
    timestamp_kind = "unknown"  # 'created' is undocumented; see the module docstring
    server_side_search = True
    paginated = True

    def __init__(
        self,
        *,
        app_id: str | None,
        app_key: str | None,
        skills_vocabulary: list[str],
        user_agent: str,
        country: str = "in",
        max_days_old: int = 2,
        results_per_page: int = 50,
        max_pages: int = 2,
        request_delay_seconds: float = 3.0,
        max_requests: int = 60,
        location_queries: dict[str, str] | None = None,
        created_is_publication: bool = False,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not app_id or not app_key:
            raise ValueError(
                "Adzuna needs your own API key: set ADZUNA_APP_ID and ADZUNA_APP_KEY in .env "
                "(free at https://developer.adzuna.com/)"
            )
        self._auth = {"app_id": app_id, "app_key": app_key}
        self.country = country
        self.site = _SITE.get(country, f"https://www.adzuna.{country}")
        self.max_days_old = max_days_old
        self.results_per_page = results_per_page
        self.max_pages = max_pages
        self.location_queries = location_queries or {}
        self.created_is_publication = created_is_publication
        if created_is_publication:
            self.timestamp_kind = "created"
        self.skills_vocabulary = skills_vocabulary
        self.last_pages = 0
        self.http = PoliteHttp(
            "Adzuna",
            user_agent=user_agent,
            delay_seconds=request_delay_seconds,
            max_requests=max_requests,
            client=client,
            sleep=sleep,
            secrets=[app_id, app_key],
        )

    def plan(self, query: SearchQuery) -> list[SearchTask]:
        """One search per configured role term x place ("Remote" is not a place to Adzuna)."""
        places = [loc for loc in query.locations if loc.strip().lower() != "remote"]
        return [
            SearchTask(
                label=f"Adzuna '{term}' in {place}",
                params={"what": term, "where": self.location_queries.get(place, place)},
            )
            for term in query.terms
            for place in places
        ]

    def search_jobs(self, task: SearchTask, query: SearchQuery) -> list[RawJob]:
        out: list[RawJob] = []
        self.last_pages = 0
        for page in range(1, self.max_pages + 1):
            data = self.http.get_json(
                f"{API_BASE}/{self.country}/search/{page}",
                params={
                    **self._auth,
                    "what": task.params["what"],
                    "where": task.params["where"],
                    "max_days_old": self.max_days_old,
                    "results_per_page": self.results_per_page,
                    "content-type": "application/json",
                },
            )
            self.last_pages += 1
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list):
                raise SourceError("Adzuna returned an unexpected payload")
            for i, job in enumerate(results):
                if isinstance(job, dict) and job.get("id") is not None:
                    out.append(RawJob(source_job_id=str(job["id"]), payload=job))
                else:  # kept so the pipeline counts it as a malformed record
                    out.append(RawJob(source_job_id=f"malformed:{page}:{i}", payload={}))
            if len(results) < self.results_per_page:
                break
        return out

    def get_job(self, source_job_id: str) -> RawJob | None:
        return None  # the search result carries every field the API offers

    def normalize_job(self, raw: RawJob) -> JobCandidate:
        p: dict[str, Any] = raw.payload
        if p.get("id") is None or not str(p.get("id")).strip():
            raise ValueError("Adzuna record has no id")
        job_id = str(p["id"]).strip()
        loc: dict[str, Any] = p["location"] if isinstance(p.get("location"), dict) else {}
        area = [a for a in loc.get("area") or [] if isinstance(a, str)]
        location = loc.get("display_name") or (", ".join(reversed(area)) if area else None)
        if location and "India" in area and "india" not in location.lower():
            location = f"{location}, India"
        description, requirements = html_to_text(p.get("description"))
        low, high, currency, period, salary_raw = adzuna_salary(p)
        posted_at, kind = self._post_date(p.get("created"))
        title = (p.get("title") or "").strip()
        url = f"{self.site}/details/{job_id}"
        company: dict[str, Any] = p["company"] if isinstance(p.get("company"), dict) else {}
        return JobCandidate(
            source=self.name,
            source_job_id=job_id,
            company=(company.get("display_name") or "").strip() or None,
            title=title,
            location=location,
            remote_type=detect_remote_type(location, title, description),
            salary_min=low,
            salary_max=high,
            salary_currency=currency,
            salary_period=period,
            salary_raw=salary_raw,
            experience_required=extract_experience(description),
            description=description,
            requirements=requirements,
            skills=extract_skills(description, self.skills_vocabulary),
            url=url,
            application_url=url,
            posted_at=posted_at,
            posted_at_kind=kind,
            raw={
                "id": job_id,
                "adzuna_created": p.get("created"),
                "category": p.get("category"),
                "contract_type": p.get("contract_type"),
                "contract_time": p.get("contract_time"),
                "location": loc or None,
            },
        )

    def _post_date(self, value: Any) -> tuple[Any, str]:
        """(posted_at, posted_at_kind). Adzuna's 'created' is undocumented: unknown unless the
        user opted in; a value without a timezone is stored as its earliest possible instant."""
        if not self.created_is_publication:
            return None, "unknown"
        aware = parse_datetime(value)
        if aware is not None:
            return aware, "created"
        naive = parse_naive_as_earliest(value)
        if naive is not None:
            return naive, "published_tz_unknown"
        return None, "unknown"

    def close(self) -> None:
        self.http.close()


def adzuna_salary(
    p: dict[str, Any],
) -> tuple[float | None, float | None, str | None, str | None, dict[str, Any] | None]:
    """(annual_min, annual_max, currency, period, raw) under PIE's salary rules.

    - salary_is_predicted != "0" (an Adzuna estimate, or no flag at all): UNKNOWN
    - numbers without a stated period (the API documents none): UNKNOWN
    - explicit text with a period ("₹8,00,000/year", "₹80,000/month"): normalized
      (yearly kept, monthly x12, hourly UNKNOWN); anything else: UNKNOWN
    The original values are always returned as ``raw`` when any salary field is present."""
    fields: dict[str, Any] = {
        k: p.get(k) for k in ("salary_min", "salary_max", "salary_is_predicted") if k in p
    }
    raw = fields if any(p.get(k) is not None for k in ("salary_min", "salary_max")) else None
    if raw is None or str(p.get("salary_is_predicted")) != "0":
        return None, None, None, None, raw
    parsed = [parse_salary_text(v) if isinstance(v, str) else (None, None, None)
              for v in (p.get("salary_min"), p.get("salary_max"))]  # fmt: skip
    periods = {period for _, _, period in parsed if period}
    currencies = {cur for _, cur, _ in parsed if cur}
    if len(periods) != 1 or len(currencies) != 1:
        return None, None, None, None, raw
    low, high, currency, period = normalize_salary(
        parsed[0][0], parsed[1][0], currencies.pop(), periods.pop()
    )
    return low, high, currency, period, raw
