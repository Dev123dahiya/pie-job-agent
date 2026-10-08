"""The JobSource contract every adapter implements.

The core pipeline (plan -> search -> normalize -> filter -> dedupe -> store) only ever sees
these types. Source-specific HTML/API details stay inside each adapter.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import AwareDatetime, BaseModel, Field, field_validator

from database.enums import RemoteType


class JobCandidate(BaseModel):
    """A normalized posting. Unknown fields are None/empty - never guessed."""

    source: str
    source_job_id: str
    company: str | None = None
    title: str
    location: str | None = None
    remote_type: RemoteType = RemoteType.UNKNOWN
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    experience_required: str | None = None
    description: str | None = None
    requirements: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    url: str
    application_url: str | None = None
    company_url: str | None = None
    posted_at: AwareDatetime | None = None
    # What posted_at really is (see TIMESTAMP_KINDS). Only a publication-type timestamp may
    # make a job "fresh"; an update time is kept separately and never used as a post date.
    posted_at_kind: str = "unknown"
    source_updated_at: AwareDatetime | None = None
    # Salary as disclosed: salary_min/max are ANNUAL amounts only when the source states a
    # yearly or monthly figure (monthly x 12); otherwise they stay None. The source's original
    # value is kept in salary_raw. Nothing is ever estimated.
    salary_period: str | None = None
    salary_raw: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None

    @field_validator("title", "source_job_id", "url")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("salary_max")
    @classmethod
    def _salary_range(cls, value: float | None, info: Any) -> float | None:
        low = info.data.get("salary_min")
        if value is not None and low is not None and value < low:
            raise ValueError("salary_max is below salary_min")
        return value


class LeadRecord(BaseModel):
    """A posting as an UNTRUSTED aggregator (Adzuna, Himalayas) reports it. Never a job:
    it only helps find the employer's trusted ATS board. `aggregator_timestamp` is kept for
    reference and is never a post date."""

    source: str
    source_lead_id: str
    company: str | None = None
    company_hint: str | None = None  # e.g. the aggregator's company slug
    title: str
    location: str | None = None
    description: str | None = None
    url: str | None = None  # query string is stripped before storage
    aggregator_timestamp: AwareDatetime | None = None
    aggregator_job_id: str | None = None  # the aggregator's own jobs row id, if any


class SearchQuery(BaseModel):
    """What to look for. Built from config/job_sources.yaml, never hardcoded."""

    terms: list[str]
    locations: list[str]
    term_aliases: dict[str, list[str]] = Field(default_factory=dict)
    # location name -> regex patterns matched against a posting's location text
    location_patterns: dict[str, list[str]] = Field(default_factory=dict)
    # Diagnostics only: discovery COUNTS postings from excluded companies and stale/undated
    # postings so reports show them. The exclusion and freshness rules themselves still run
    # at analysis/shortlisting exactly as before.
    excluded_companies: list[str] = Field(default_factory=list)
    max_age_hours: int | None = None


class SearchTask(BaseModel):
    """One unit of searching (one board, one term+location pair, ...). Progress is per task."""

    label: str
    params: dict[str, Any] = Field(default_factory=dict)


class RawJob(BaseModel):
    """A source-native posting before normalization."""

    source_job_id: str
    payload: dict[str, Any]


class SourceError(Exception):
    """A source failed for this task; discovery logs it and continues with other tasks."""


class SourceAuthRequired(SourceError):
    """The source needs the user to sign in. Never bypassed; surfaced to the dashboard."""

    def __init__(self, message: str, user_action: str) -> None:
        super().__init__(message)
        self.user_action = user_action


class SourceRateLimited(SourceError):
    """The source asked us to slow down; we stop querying it for this run."""


class SourceTimeout(SourceError):
    """The source did not answer in time (after bounded retries)."""


# posted_at_kind values:
#   published             the source's publication time (Greenhouse first_published, Ashby
#                         publishedAt, Remotive publication_date)
#   released              SmartRecruiters releasedDate (the posting's release = publication)
#   created               the posting's creation time (Lever createdAt, Adzuna created). A
#                         posting is published at or after creation, so age is never understated
#   published_tz_unknown  a publication time without a timezone; stored as the EARLIEST
#                         instant it could be (UTC-14h), so it can never look fresher than it is
#   updated_only          the source gives only a last-updated time: posted_at stays None
#                         (freshness UNKNOWN) and the update time goes to source_updated_at
#   relative_upper_bound  an age like "2 days ago" (Naukri, LinkedIn), stored as the OLDEST
#                         instant it can mean, so age is never understated
#   unknown               no usable timestamp: freshness UNKNOWN
TIMESTAMP_KINDS = (
    "published",
    "released",
    "created",
    "published_tz_unknown",
    "updated_only",
    "relative_upper_bound",
    "unknown",
)


class JobSource(ABC):
    """Adapter interface. Implementations must be read-only and respect the site's rules."""

    name: str
    requires_auth: bool = False
    # Self-description (shown in reports and the dashboard).
    discovery_method: str = ""
    docs_url: str = ""
    timestamp_kind: str = "unknown"
    server_side_search: bool = False  # can the source filter by keyword/location itself?
    paginated: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "discovery_method": self.discovery_method,
            "docs_url": self.docs_url,
            "timestamp_kind": self.timestamp_kind,
            "server_side_search": self.server_side_search,
            "paginated": self.paginated,
            "requires_auth": self.requires_auth,
        }

    @abstractmethod
    def plan(self, query: SearchQuery) -> list[SearchTask]:
        """Split a query into search tasks."""

    @abstractmethod
    def search_jobs(self, task: SearchTask, query: SearchQuery) -> list[RawJob]:
        """Run one search task. May pre-filter; the core filters again uniformly."""

    @abstractmethod
    def get_job(self, source_job_id: str) -> RawJob | None:
        """Fetch one posting by id (None if it no longer exists)."""

    @abstractmethod
    def normalize_job(self, raw: RawJob) -> JobCandidate:
        """Map a raw posting to a JobCandidate. Raise ValueError on unusable data."""

    def enrich(self, raw: RawJob) -> RawJob:
        """Optionally fetch extra detail for a posting the core decided to keep."""
        return raw

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release network resources."""


def parse_datetime(value: Any) -> datetime | None:
    """ISO-8601 with offset -> aware datetime; anything unparseable or naive -> None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def parse_epoch_millis(value: Any) -> datetime | None:
    """Unix epoch milliseconds (Lever createdAt) -> aware UTC datetime; junk -> None."""
    from datetime import UTC

    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_naive_as_earliest(value: Any) -> datetime | None:
    """A timestamp without a timezone could be anywhere from UTC-12 to UTC+14. Return the
    EARLIEST instant it can mean (as if written at UTC+14), so freshness is never overstated."""
    from datetime import UTC, timedelta

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed
    return parsed.replace(tzinfo=UTC) - timedelta(hours=14)
