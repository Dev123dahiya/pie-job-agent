"""The sources PIE trusts for freshness, checked the same way:

    publication time = now - 24h           -> FRESH
    publication time = now - 48h           -> FRESH   (boundary)
    publication time = now - 48h - 1s      -> STALE
    publication time missing               -> UNKNOWN -> rejected
    only an UPDATE time (recent)           -> UNKNOWN -> rejected

Greenhouse first_published, Lever createdAt (creation precedes publication, so a job created
within 48h was published within 48h), Ashby publishedAt, SmartRecruiters releasedDate.
Rejection uses the SHIPPED config/shortlist_rules.yaml. HTTP is never touched."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx2 as httpx
import pytest

from agent.analysis.schema import JobAnalysisOutput
from backend.core.config import get_settings
from backend.services.freshness import compute_freshness
from backend.services.shortlist import evaluate, load_shortlist_rules
from database.enums import Freshness
from integrations.job_sources.ashby import AshbySource
from integrations.job_sources.base import JobCandidate, RawJob
from integrations.job_sources.greenhouse import GreenhouseSource
from integrations.job_sources.lever import LeverSource
from integrations.job_sources.smartrecruiters import SmartRecruitersSource

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)
CLIENT = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def greenhouse(published: datetime | None, updated: datetime | None) -> JobCandidate:
    src = GreenhouseSource(["b"], skills_vocabulary=[], user_agent="t", client=CLIENT)
    payload = {
        "id": 1,
        "title": "Software Engineer",
        "absolute_url": "https://x.test/1",
        "location": {"name": "Noida"},
        "_board": "b",
        "first_published": iso(published) if published else None,
        "updated_at": iso(updated) if updated else None,
    }
    return src.normalize_job(RawJob(source_job_id="b:1", payload=payload))


def lever(published: datetime | None, updated: datetime | None) -> JobCandidate:
    src = LeverSource(["acme"], skills_vocabulary=[], user_agent="t", client=CLIENT)
    payload = {
        "id": "1",
        "text": "Software Engineer",
        "categories": {"location": "Noida"},
        "country": "IN",
        "hostedUrl": "https://jobs.lever.co/acme/1",
        "_company": "acme",
        "createdAt": int(published.timestamp() * 1000) if published else None,
        "updatedAt": int(updated.timestamp() * 1000) if updated else None,
    }
    return src.normalize_job(RawJob(source_job_id="acme:1", payload=payload))


def ashby(published: datetime | None, updated: datetime | None) -> JobCandidate:
    src = AshbySource(["acme"], skills_vocabulary=[], user_agent="t", client=CLIENT)
    payload = {
        "id": "1",
        "title": "Software Engineer",
        "location": "Noida",
        "_board": "acme",
        "jobUrl": "https://jobs.ashbyhq.com/acme/1",
        "publishedAt": iso(published) if published else None,
        "updatedAt": iso(updated) if updated else None,
    }
    return src.normalize_job(RawJob(source_job_id="acme:1", payload=payload))


def smartrecruiters(published: datetime | None, updated: datetime | None) -> JobCandidate:
    src = SmartRecruitersSource(["Acme"], skills_vocabulary=[], user_agent="t", client=CLIENT)
    payload = {
        "id": "1",
        "name": "Software Engineer",
        "_company": "Acme",
        "location": {"city": "Noida", "country": "in"},
        "releasedDate": iso(published) if published else None,
        "updatedOn": iso(updated) if updated else None,
    }
    return src.normalize_job(RawJob(source_job_id="Acme:1", payload=payload))


SOURCES: dict[str, Callable[[datetime | None, datetime | None], JobCandidate]] = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
}


def freshness(job: JobCandidate) -> Freshness:
    return compute_freshness(job.posted_at, NOW, 48)


def rejected_for_freshness(level: Freshness) -> bool:
    rules = load_shortlist_rules(get_settings().shortlist_rules_path)
    output = JobAnalysisOutput.model_validate(
        {
            "role_match": "strong",
            "skills_match": "strong",
            "experience_match": "strong",
            "location_match": "strong",
            "salary_match": "unknown",
            "freshness": level.value,
            "matching_skills": [],
            "missing_skills": [],
            "important_requirements": [],
            "concerns": [],
            "match_score": 90,
            "should_shortlist": True,
            "reason": "ok",
        }
    )
    ok, results = evaluate(output, rules)
    return not ok and [r["rule"] for r in results if not r["passed"]] == ["freshness"]


@pytest.mark.parametrize("name", SOURCES)
@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(hours=24), Freshness.FRESH),
        (timedelta(hours=48), Freshness.FRESH),
        (timedelta(hours=48, seconds=1), Freshness.STALE),
    ],
)
def test_publication_time_decides_freshness(name: str, age: timedelta, expected: Freshness) -> None:
    job = SOURCES[name](NOW - age, None)
    assert job.posted_at_kind in ("published", "created", "released")
    assert freshness(job) is expected
    assert rejected_for_freshness(expected) is (expected is not Freshness.FRESH)


@pytest.mark.parametrize("name", SOURCES)
def test_missing_publication_time_is_unknown_and_rejected(name: str) -> None:
    job = SOURCES[name](None, None)
    assert job.posted_at is None and job.posted_at_kind == "unknown"
    assert freshness(job) is Freshness.UNKNOWN and rejected_for_freshness(Freshness.UNKNOWN)


@pytest.mark.parametrize("name", SOURCES)
def test_update_time_never_substitutes_for_publication(name: str) -> None:
    job = SOURCES[name](None, NOW - timedelta(hours=1))  # updated an hour ago, never published
    assert job.posted_at is None and freshness(job) is Freshness.UNKNOWN
    assert rejected_for_freshness(Freshness.UNKNOWN)


def test_greenhouse_update_time_is_kept_separately_not_as_post_date() -> None:
    job = greenhouse(NOW - timedelta(hours=72), NOW - timedelta(hours=1))
    assert freshness(job) is Freshness.STALE  # old posting, recently updated: still stale
    assert job.source_updated_at == NOW - timedelta(hours=1)


@pytest.mark.parametrize("name", ["greenhouse", "ashby", "smartrecruiters"])
def test_timestamps_without_a_timezone_are_not_trusted(name: str) -> None:
    naive = (NOW - timedelta(hours=1)).replace(tzinfo=None)  # "2026-09-29T11:00:00", no zone
    job = SOURCES[name](naive, None)
    assert job.posted_at is None and job.posted_at_kind == "unknown"
    assert freshness(job) is Freshness.UNKNOWN and rejected_for_freshness(Freshness.UNKNOWN)
