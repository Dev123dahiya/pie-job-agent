"""Phase 5.7: strict same-job matching between an aggregator lead and trusted ATS postings.

Pure function tests (no DB, no network). Candidates are postings from the employer's VERIFIED
board; matching only decides identity - it never moves a timestamp."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from backend.core.config import get_settings
from backend.core.normalize import canonicalize_url
from backend.services.dedupe import (
    SameJobSubject,
    description_overlap,
    explicit_ats_ids,
    match_same_job,
)
from backend.services.discovery import location_matches
from database.models import Job
from integrations.job_sources.config import load_job_sources_config

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
DESC = (
    "Acme Tech is hiring a Software Engineer to build backend services in Python and FastAPI. "
    "You will design REST APIs, write tests, work with PostgreSQL and ship features to "
    "millions of users across India. We value ownership, clear writing and pragmatic "
    "engineering. Freshers with strong fundamentals in data structures are welcome to apply."
)
QUERY = load_job_sources_config(get_settings().job_sources_path).build_query([], [])


def region(loc: str | None) -> str | None:
    return location_matches(loc, QUERY)


def job(
    title: str = "Software Engineer",
    location: str = "Bengaluru, India",
    posted: datetime | None = NOW - timedelta(hours=30),
    description: str | None = DESC,
    sid: str = "acmetech:101",
    url: str = "https://job-boards.greenhouse.io/acmetech/jobs/101",
) -> Job:
    return Job(
        id=uuid.uuid4(),
        source="greenhouse",
        source_job_id=sid,
        title=title,
        location=location,
        posted_at=posted,
        description=description,
        url=url,
        canonical_url=canonicalize_url(url),
    )


def lead(
    title: str = "Software Engineer",
    location: str | None = "Bangalore, Karnataka, India",
    description: str | None = DESC,
    first_seen: datetime = NOW,
    url: str | None = "https://www.adzuna.in/details/555",
) -> SameJobSubject:
    return SameJobSubject(
        title=title, location=location, description=description, first_seen_at=first_seen, url=url
    )


def test_one_strong_unique_candidate_matches() -> None:
    target = job()
    other = job(title="Data Engineer", sid="acmetech:102")
    result = match_same_job(lead(), [target, other], region)
    assert result.status == "MATCH" and result.job is target
    assert result.evidence["location"] == "city" and result.evidence["description_overlap"] == 1.0


def test_same_title_in_another_city_is_no_match() -> None:
    result = match_same_job(lead(), [job(location="Hyderabad, India")], region)
    assert result.status == "NO_MATCH" and result.evidence["rule"] == "location"


def test_identical_candidates_are_ambiguous_never_picked() -> None:
    a, b = job(sid="acmetech:1"), job(sid="acmetech:2")
    result = match_same_job(lead(), [a, b], region)
    assert result.status == "AMBIGUOUS" and result.job is None


@pytest.mark.parametrize(
    "title", ["Software Engineer II", "Senior Software Engineer", "Software Engineer - Backend"]
)
def test_similar_title_is_no_match(title: str) -> None:
    assert match_same_job(lead(), [job(title=title)], region).status == "NO_MATCH"


def test_alias_title_is_no_match() -> None:
    # "Software Development Engineer" is a configured alias of SDE - still not the same title.
    result = match_same_job(lead(title="SDE"), [job(title="Software Development Engineer")], region)
    assert result.status == "NO_MATCH" and result.evidence["rule"] == "title"


def test_low_description_overlap_is_no_match() -> None:
    other = (
        "We are an insurance marketplace looking for an engineer to maintain legacy Java "
        "systems, on-call rotations, and Oracle stored procedures. Experience with mainframes "
        "and COBOL is a plus. This role supports claims processing across Europe and needs "
        "five years of enterprise experience plus people management responsibilities."
    )
    result = match_same_job(lead(), [job(description=other)], region)
    assert result.status == "NO_MATCH" and result.evidence["failed"] == "description_overlap"


def test_exact_ats_id_matches_even_without_title_or_timing() -> None:
    target = job(title="Totally Different Title", posted=NOW - timedelta(days=200))
    subject = lead(
        description=f"Apply at https://boards.greenhouse.io/acmetech/jobs/101 today. {DESC}"
    )
    result = match_same_job(subject, [target, job(sid="acmetech:999")], region)
    assert result.status == "MATCH" and result.job is target
    assert result.evidence == {"rule": "explicit_identity"}


def test_exact_canonical_url_matches() -> None:
    target = job(sid="acmetech:7", url="https://careers.acme.example/jobs/7")
    subject = lead(url="https://careers.acme.example/jobs/7?utm_source=adzuna")
    assert match_same_job(subject, [target], region).job is target


# --- timing: the lead's own time never sets freshness; it only guards identity -------------


def test_ats_posting_older_than_the_lead_can_match() -> None:
    # Aggregators list postings after the ATS publishes them (observed lag >= ~43h).
    for hours in (30, 50, 24 * 120):
        target = job(posted=NOW - timedelta(hours=hours))
        result = match_same_job(lead(first_seen=NOW), [target], region)
        assert result.status == "MATCH"
        assert target.posted_at == NOW - timedelta(hours=hours)  # untouched


def test_ats_posting_newer_than_lead_plus_tolerance_is_a_repost_not_a_match() -> None:
    at_boundary = job(posted=NOW + timedelta(hours=6))
    assert match_same_job(lead(first_seen=NOW), [at_boundary], region).status == "MATCH"
    past = job(posted=NOW + timedelta(hours=6, seconds=1))
    result = match_same_job(lead(first_seen=NOW), [past], region)
    assert result.status == "NO_MATCH" and result.evidence["failed"] == "ats_newer_than_lead"


def test_ats_posting_without_trusted_date_is_no_match() -> None:
    result = match_same_job(lead(), [job(posted=None)], region)
    assert result.status == "NO_MATCH" and result.evidence["failed"] == "no_trusted_ats_date"


def test_without_description_a_city_match_is_required() -> None:
    remote = job(location="Remote - India")
    result = match_same_job(lead(location="India (Remote)", description=None), [remote], region)
    assert result.status == "NO_MATCH" and result.evidence["failed"] == "needs_city_or_description"
    assert match_same_job(lead(description=None), [job()], region).status == "MATCH"


def test_helpers() -> None:
    assert explicit_ats_ids(
        "see https://jobs.lever.co/acme/0f8e3c1a-1111-2222-3333-444455556666"
    ) == {"acme:0f8e3c1a-1111-2222-3333-444455556666"}
    assert explicit_ats_ids("https://jobs.smartrecruiters.com/Acme/7433-backend-engineer") == {
        "acme:7433"
    }
    assert description_overlap(DESC, DESC) == 1.0 and description_overlap(None, DESC) == 0.0
