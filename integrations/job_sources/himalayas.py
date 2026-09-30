"""Himalayas remote-jobs API as a LEAD source (Phase 5.7). Not a JobSource: its records are
stored as job_leads only and never become jobs.

Why only leads: its `pubDate` is Himalayas' own listing time. Checked against 55 postings also
on the employer's ATS, it was never earlier than the ATS publication (>= ~43h later) and often
re-dated postings that were months or years old. It is kept as an untrusted reference value.

Documented public search API (https://himalayas.app/docs/remote-jobs-api), no key. Only the
search endpoint is called - never `applicationLink` or any redirect/employer page. Terms:
attribution to Himalayas with a link back (kept on every lead), data refreshes daily.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx2 as httpx

from backend.core.normalize import html_to_text
from integrations.job_sources.base import LeadRecord, SourceError, SourceRateLimited, parse_datetime
from integrations.job_sources.config import HimalayasLeadConfig
from integrations.job_sources.http import PoliteHttp

SEARCH_URL = "https://himalayas.app/jobs/api/search"
ATTRIBUTION = "Job data from Himalayas (https://himalayas.app)"
MAX_DESCRIPTION = 4000


def strip_query(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def untrusted_timestamp(value: Any) -> datetime | None:
    """pubDate as a reference value only (seconds, milliseconds or ISO-8601 with offset)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return parse_datetime(value)


def lead_from_item(item: dict[str, Any], country_name: str = "India") -> LeadRecord | None:
    """A Himalayas job -> LeadRecord, or None if it is not open to the country or unusable.
    Worldwide listings (no restriction) are skipped: a bare "remote" is not remote-India."""
    guid = strip_query(item.get("guid"))
    title = (item.get("title") or "").strip()
    restrictions = [str(r) for r in item.get("locationRestrictions") or []]
    if not guid or not title or country_name.lower() not in (r.lower() for r in restrictions):
        return None
    description, _ = html_to_text(item.get("description"))
    return LeadRecord(
        source="himalayas",
        source_lead_id=guid,
        company=(item.get("companyName") or "").strip() or None,
        company_hint=(item.get("companySlug") or "").strip() or None,
        title=title,
        location=f"Remote - {country_name}",
        description=(description or "")[:MAX_DESCRIPTION] or None,
        url=guid,
        aggregator_timestamp=untrusted_timestamp(item.get("pubDate")),
    )


class HimalayasLeadSource:
    name = "himalayas"
    attribution = ATTRIBUTION

    def __init__(
        self,
        config: HimalayasLeadConfig,
        terms: list[str],
        *,
        user_agent: str,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.terms = config.terms or terms
        self.http = PoliteHttp(
            "Himalayas",
            user_agent=user_agent,
            delay_seconds=config.request_delay_seconds,
            max_requests=config.max_requests_per_run,
            client=client,
            sleep=sleep,
        )
        self.errors: list[str] = []
        self.rate_limited = False

    def collect(self) -> list[LeadRecord]:
        country = {"IN": "India"}.get(self.config.country.upper(), self.config.country)
        seen: dict[str, LeadRecord] = {}
        for term in self.terms:
            for page in range(1, self.config.max_pages + 1):
                params: dict[str, Any] = {"q": term, "country": self.config.country, "page": page}
                if self.config.seniority:
                    params["seniority"] = ",".join(self.config.seniority)
                try:
                    data = self.http.get_json(SEARCH_URL, params=params)
                except SourceRateLimited as exc:
                    self.rate_limited = "429" in str(exc)
                    self.errors.append(str(exc))
                    return list(seen.values())
                except SourceError as exc:
                    self.errors.append(str(exc))
                    break
                items = (data or {}).get("jobs") if isinstance(data, dict) else None
                if not isinstance(items, list) or not items:
                    break
                for item in items:
                    lead = lead_from_item(item, country) if isinstance(item, dict) else None
                    if lead is not None:
                        seen.setdefault(lead.source_lead_id, lead)
                if len(items) < 20:  # the API's page size
                    break
        return list(seen.values())

    def close(self) -> None:
        self.http.close()
