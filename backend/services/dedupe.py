"""Duplicate detection for discovered jobs.

1. Primary:   same source + source_job_id           (the same posting seen again)
2. Secondary: same canonical URL                    (tracking params/fragments ignored)
3. Secondary: same fingerprint                      (normalized company | title | location:
                                                     a re-post under a new id, or the same
                                                     role listed on another source)
4. Cross-source: same company + normalized title + the same single city, on a DIFFERENT
   source (e.g. Greenhouse "Gurugram" vs Adzuna "Gurgaon, Haryana, India"). Titles must be
   identical after normalization, so "Software Engineer I" and "... II" never merge, and a
   location naming no known city (or several) never matches this way.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.normalize import canonicalize_url, city_key, job_fingerprint, normalize_title
from database.models import Company, Job
from integrations.job_sources.base import JobCandidate

DuplicateReason = Literal["source_id", "canonical_url", "fingerprint", "company_title_city"]


@dataclass(frozen=True)
class DedupeKeys:
    canonical_url: str | None
    fingerprint: str

    @classmethod
    def for_candidate(cls, candidate: JobCandidate) -> "DedupeKeys":
        return cls(
            canonical_url=canonicalize_url(candidate.url),
            fingerprint=job_fingerprint(candidate.company, candidate.title, candidate.location),
        )


@dataclass(frozen=True)
class DuplicateMatch:
    job: Job
    reason: DuplicateReason


def find_duplicate(db: Session, candidate: JobCandidate, keys: DedupeKeys) -> DuplicateMatch | None:
    job = db.scalar(
        select(Job).where(
            Job.source == candidate.source, Job.source_job_id == candidate.source_job_id
        )
    )
    if job is not None:
        return DuplicateMatch(job, "source_id")
    if keys.canonical_url:
        job = db.scalar(select(Job).where(Job.canonical_url == keys.canonical_url).limit(1))
        if job is not None:
            return DuplicateMatch(job, "canonical_url")
    job = db.scalar(select(Job).where(Job.fingerprint == keys.fingerprint).limit(1))
    if job is not None:
        return DuplicateMatch(job, "fingerprint")
    city = city_key(candidate.location)
    if candidate.company and city:
        title = normalize_title(candidate.title)
        same_company = db.scalars(
            select(Job)
            .join(Company, Company.id == Job.company_id)
            .where(
                Company.normalized_name == Company.normalize_name(candidate.company),
                Job.source != candidate.source,
            )
        ).all()
        for other in same_company:
            if normalize_title(other.title) == title and city_key(other.location) == city:
                return DuplicateMatch(other, "company_title_city")
    return None


# --- Phase 5.7: aggregator leads vs trusted ATS postings -----------------------------------

# Sources whose timestamps PIE never trusts. Their records are leads: they can point PIE at an
# employer's ATS board, but never make a job fresh.
AGGREGATOR_SOURCES = frozenset({"adzuna", "himalayas"})

_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
# Public posting URLs of the supported ATSs -> the adapter's source_job_id ("<board>:<id>").
_ATS_URL_IDS = [
    ("greenhouse", re.compile(r"(?:job-)?boards\.greenhouse\.io/([\w-]+)/jobs/(\d+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([\w.-]+)/([0-9a-f-]{36})", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)/([0-9a-f-]{36})", re.I)),
    ("smartrecruiters", re.compile(r"jobs\.smartrecruiters\.com/([\w.-]+)/(\d+)", re.I)),
]
_WORD = re.compile(r"[a-z0-9]+")

MatchStatus = Literal["MATCH", "NO_MATCH", "AMBIGUOUS"]


@dataclass(frozen=True)
class SameJobSubject:
    """What an aggregator says about a posting (a lead, or an aggregator job row)."""

    title: str
    location: str | None
    description: str | None
    first_seen_at: datetime  # when PIE first saw it on the aggregator - PIE's own clock
    url: str | None = None


@dataclass(frozen=True)
class SameJobResult:
    status: MatchStatus
    job: Job | None
    evidence: dict[str, Any]


def explicit_ats_ids(*texts: str | None) -> set[str]:
    """source_job_ids ('<board>:<id>') of supported-ATS posting URLs quoted in the texts."""
    found: set[str] = set()
    for text in texts:
        for _, pattern in _ATS_URL_IDS:
            for board, job_id in pattern.findall(text or ""):
                found.add(f"{board.lower()}:{job_id.lower()}")
    return found


def explicit_ats_boards(*texts: str | None) -> set[tuple[str, str]]:
    """(platform, board id) of supported-ATS posting URLs quoted in the texts."""
    found: set[tuple[str, str]] = set()
    for text in texts:
        for platform, pattern in _ATS_URL_IDS:
            for board, _ in pattern.findall(text or ""):
                found.add((platform, board))
    return found


def _canonical_urls(*texts: str | None) -> set[str]:
    return {u for t in texts for m in _URL.findall(t or "") if (u := canonicalize_url(m))}


def description_overlap(lead_text: str | None, job_text: str | None, size: int = 5) -> float:
    """Share of the lead's word 5-shingles that also occur in the job text (0..1)."""

    def shingles(text: str | None) -> set[tuple[str, ...]]:
        words = _WORD.findall((text or "").lower())
        return {tuple(words[i : i + size]) for i in range(len(words) - size + 1)}

    lead, job = shingles(lead_text), shingles(job_text)
    return len(lead & job) / len(lead) if lead else 0.0


def match_same_job(
    subject: SameJobSubject,
    candidates: list[Job],
    region_of: Callable[[str | None], str | None],
    *,
    tolerance: timedelta = timedelta(hours=6),
    min_description_chars: int = 300,
    min_overlap: float = 0.5,
) -> SameJobResult:
    """Is one of `candidates` (postings from the employer's VERIFIED board) the same job?

    Explicit identity (an ATS posting URL/id or the same canonical URL) decides on its own.
    Otherwise every rule must hold for exactly ONE candidate:
      title     identical after normalization - no fuzzy matching, no aliases
      location  the same city; or, if either names no city, the same configured location
      plausible >1 candidate with that title + location -> AMBIGUOUS (never picked)
      timing    the ATS posting has its own trusted date and is not newer than the lead's
                first sighting + tolerance (a posting newer than the lead is a repost)
      content   with a lead description of >= min_description_chars, at least min_overlap
                of its 5-shingles occur in the posting; without one, a city-level match
    Nothing is copied between records: this only decides whether they are the same job.
    """
    by_id = explicit_ats_ids(subject.url, subject.description)
    urls = _canonical_urls(subject.url, subject.description)
    explicit = [
        j for j in candidates
        if j.source_job_id.lower() in by_id or (j.canonical_url and j.canonical_url in urls)
    ]  # fmt: skip
    if len(explicit) == 1:
        return SameJobResult("MATCH", explicit[0], {"rule": "explicit_identity"})
    if len(explicit) > 1:
        return SameJobResult(
            "AMBIGUOUS", None, {"rule": "explicit_identity", "candidates": len(explicit)}
        )

    title = normalize_title(subject.title)
    same_title = [j for j in candidates if normalize_title(j.title) == title]
    if not same_title:
        return SameJobResult("NO_MATCH", None, {"rule": "title", "candidates": len(candidates)})

    lead_city, lead_region = city_key(subject.location), region_of(subject.location)
    plausible: list[tuple[Job, str]] = []
    for job in same_title:
        job_city = city_key(job.location)
        if lead_city and job_city:
            if lead_city == job_city:
                plausible.append((job, "city"))
        elif lead_region and lead_region == region_of(job.location):
            plausible.append((job, "region"))
    if not plausible:
        return SameJobResult("NO_MATCH", None, {"rule": "location", "same_title": len(same_title)})
    if len(plausible) > 1:
        return SameJobResult("AMBIGUOUS", None, {"rule": "unique", "candidates": len(plausible)})

    job, location_level = plausible[0]
    evidence: dict[str, Any] = {"rule": "strict", "location": location_level}
    if job.posted_at is None:
        return SameJobResult("NO_MATCH", None, {**evidence, "failed": "no_trusted_ats_date"})
    if job.posted_at > subject.first_seen_at + tolerance:
        return SameJobResult("NO_MATCH", None, {**evidence, "failed": "ats_newer_than_lead"})
    if subject.description and len(subject.description) >= min_description_chars:
        overlap = description_overlap(subject.description, job.description)
        evidence["description_overlap"] = round(overlap, 3)
        if overlap < min_overlap:
            return SameJobResult("NO_MATCH", None, {**evidence, "failed": "description_overlap"})
    elif location_level != "city":
        return SameJobResult("NO_MATCH", None, {**evidence, "failed": "needs_city_or_description"})
    return SameJobResult("MATCH", job, evidence)
