"""Typed loader for config/job_sources.yaml."""

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from integrations.job_sources.base import SearchQuery


class SearchConfig(BaseModel):
    terms: list[str] = Field(default_factory=list)
    term_aliases: dict[str, list[str]] = Field(default_factory=dict)
    locations: list[str] = Field(default_factory=list)
    location_patterns: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("location_patterns")
    @classmethod
    def _valid_regex(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        for name, patterns in value.items():
            for pattern in patterns:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"Invalid regex for location {name!r}: {pattern!r}") from exc
        return value


class SourceConfig(BaseModel):
    """One configured source. Adapter-specific keys (boards, delays...) are kept as extras."""

    model_config = ConfigDict(extra="allow")

    name: str
    type: str
    enabled: bool = True

    @property
    def options(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class HimalayasLeadConfig(BaseModel):
    """Himalayas remote-jobs API as a LEAD source only: its pubDate is never a post date."""

    enabled: bool = False
    country: str = "IN"
    seniority: list[str] = Field(default_factory=lambda: ["Entry-level"])
    terms: list[str] = Field(default_factory=list)  # empty: the configured search terms
    max_pages: int = 1
    max_requests_per_run: int = 10
    request_delay_seconds: float = 2.0


class BoardOverride(BaseModel):
    """A board you vouch for: '<company as the aggregator names it>' -> platform + board id."""

    platform: str
    board: str


class LeadResolutionConfig(BaseModel):
    """Lead -> trusted ATS resolution (Phase 5.7). Off unless you turn it on."""

    enabled: bool = False
    # Boards found for leads are never crawled until you approve them, unless this is true
    # (and even then only boards whose identity was verified automatically).
    auto_activate_verified: bool = False
    max_requests_per_run: int = 60  # all board-resolution requests together
    # One company may use at most this many of them (4 platforms x its best 2 id guesses).
    max_requests_per_company: int = 8
    max_companies_per_run: int = 25  # companies that need requests; cached ones are free
    max_boards_crawled_per_run: int = 10
    request_delay_seconds: float = 1.0
    negative_cache_days: int = 14
    lead_match_days: int = 14  # keep trying to match a lead for this long after it was seen
    # Identity proof for Lever/Ashby (their APIs return no company name): at least this share
    # of at least `min_postings_for_mention` postings must name the company in their text.
    description_mention_threshold: float = 0.8
    min_postings_for_mention: int = 3
    # Same-job matching.
    match_timing_tolerance_hours: float = 6.0
    min_description_chars: int = 300
    min_description_overlap: float = 0.5
    overrides: dict[str, BoardOverride] = Field(default_factory=dict)
    himalayas: HimalayasLeadConfig = Field(default_factory=HimalayasLeadConfig)


class JobSourcesConfig(BaseModel):
    search: SearchConfig = Field(default_factory=SearchConfig)
    skills_vocabulary: list[str] = Field(default_factory=list)
    sources: list[SourceConfig] = Field(default_factory=list)
    lead_resolution: LeadResolutionConfig = Field(default_factory=LeadResolutionConfig)

    def source(self, name: str) -> SourceConfig | None:
        return next((s for s in self.sources if s.name == name), None)

    def enabled_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled]

    def build_query(
        self,
        fallback_terms: list[str],
        fallback_locations: list[str],
        *,
        excluded_companies: list[str] | None = None,
        max_age_hours: int | None = None,
    ) -> SearchQuery:
        """Search config wins; the user's role/location preferences fill any gaps."""
        return SearchQuery(
            terms=self.search.terms or fallback_terms,
            locations=self.search.locations or fallback_locations,
            term_aliases=self.search.term_aliases,
            location_patterns=self.search.location_patterns,
            excluded_companies=excluded_companies or [],
            max_age_hours=max_age_hours,
        )


def load_job_sources_config(path: Path) -> JobSourcesConfig:
    if not path.exists():
        return JobSourcesConfig()
    with path.open(encoding="utf-8") as fh:
        return JobSourcesConfig.model_validate(yaml.safe_load(fh) or {})
