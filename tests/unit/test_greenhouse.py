"""Greenhouse adapter tests. HTTP is mocked; nothing touches the real API."""

from datetime import UTC, datetime
from typing import Any

import httpx2 as httpx
import pytest
from pydantic import ValidationError

from database.enums import RemoteType
from integrations.job_sources.base import (
    RawJob,
    SearchQuery,
    SearchTask,
    SourceAuthRequired,
    SourceError,
    SourceRateLimited,
)
from integrations.job_sources.greenhouse import GreenhouseSource

QUERY = SearchQuery(terms=["Software Engineer"], locations=["Gurugram"])


def payload(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": 8063904,
        "title": "Backend Software Engineer",
        "company_name": "Agoda",
        "absolute_url": "https://job-boards.greenhouse.io/agoda/jobs/8063904",
        "location": {"name": "Gurugram"},
        "first_published": "2026-09-27T01:23:02-04:00",
        "updated_at": "2026-09-28T20:30:00-04:00",
        "content": "&lt;p&gt;Build APIs in Python.&lt;/p&gt;&lt;h3&gt;Requirements&lt;/h3&gt;"
        "&lt;ul&gt;&lt;li&gt;2+ years of experience&lt;/li&gt;"
        "&lt;li&gt;Kafka&lt;/li&gt;&lt;/ul&gt;",
        "metadata": [{"name": "Workplace", "value": "Hybrid"}],
        "_board": "agoda",
    }
    return {**base, **overrides}


def make_source(handler: Any, sleeps: list[float] | None = None, **kwargs: Any) -> GreenhouseSource:
    sleeps = sleeps if sleeps is not None else []
    return GreenhouseSource(
        ["agoda"],
        skills_vocabulary=["Python", "Kafka", "Java"],
        user_agent="test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
        request_delay_seconds=0,
        **kwargs,
    )


def test_normalize_maps_only_real_fields() -> None:
    src = make_source(lambda r: httpx.Response(200, json={}))
    job = src.normalize_job(RawJob(source_job_id="agoda:8063904", payload=payload()))
    assert job.company == "Agoda"
    assert job.location == "Gurugram"
    assert job.remote_type is RemoteType.HYBRID  # from the posting's metadata
    # first_published is the post date; updated_at must not be used.
    assert job.posted_at == datetime(2026, 9, 27, 5, 23, 2, tzinfo=UTC)
    assert job.experience_required == "2+ years"
    assert job.skills == ["Python", "Kafka"]
    assert job.requirements == ["2+ years of experience", "Kafka"]
    assert job.salary_min is None and job.salary_max is None and job.salary_currency is None
    assert job.description is not None and "&lt;" not in job.description


def test_missing_optional_fields_become_none() -> None:
    src = make_source(lambda r: httpx.Response(200, json={}))
    job = src.normalize_job(
        RawJob(
            source_job_id="agoda:1",
            payload=payload(
                location=None, first_published=None, content=None, metadata=None, company_name=None
            ),
        )
    )
    assert job.location is None and job.posted_at is None and job.description is None
    assert job.company == "agoda"  # falls back to the board name, which is the company's token
    assert job.experience_required is None and job.skills == []


def test_missing_title_is_rejected() -> None:
    src = make_source(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValidationError):
        src.normalize_job(RawJob(source_job_id="agoda:1", payload=payload(title="  ")))


def test_single_pay_range_in_cents() -> None:
    src = make_source(lambda r: httpx.Response(200, json={}))
    ranges = [{"min_cents": 120000000, "max_cents": 180000000, "currency_type": "INR"}]
    job = src.normalize_job(
        RawJob(source_job_id="agoda:1", payload=payload(pay_input_ranges=ranges))
    )
    assert (job.salary_min, job.salary_max, job.salary_currency) == (1200000.0, 1800000.0, "INR")
    # Several ranges (e.g. per region) are ambiguous: store nothing rather than guess.
    job = src.normalize_job(
        RawJob(source_job_id="agoda:1", payload=payload(pay_input_ranges=ranges * 2))
    )
    assert job.salary_min is None


def test_search_lists_board_and_sends_user_agent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"jobs": [payload(), payload(id=2)]})

    src = GreenhouseSource(
        ["agoda"],
        skills_vocabulary=[],
        user_agent="PIE-test/1",
        request_delay_seconds=0,
        sleep=lambda s: None,
    )
    src._client = httpx.Client(
        transport=httpx.MockTransport(handler), headers={"User-Agent": "PIE-test/1"}
    )
    tasks = src.plan(QUERY)
    assert [t.params["board"] for t in tasks] == ["agoda"]
    raws = src.search_jobs(tasks[0], QUERY)
    assert [r.source_job_id for r in raws] == ["agoda:8063904", "agoda:2"]
    assert seen[0].url.path == "/v1/boards/agoda/jobs"
    assert seen[0].url.params["content"] == "true"
    assert seen[0].headers["User-Agent"] == "PIE-test/1"


def test_unknown_board_is_a_source_error() -> None:
    src = make_source(lambda r: httpx.Response(404))
    with pytest.raises(SourceError, match="does not exist"):
        src.search_jobs(SearchTask(label="x", params={"board": "nope"}), QUERY)


def test_rate_limit_stops_the_source_for_the_run() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429)

    src = make_source(handler)
    task = SearchTask(label="x", params={"board": "agoda"})
    with pytest.raises(SourceRateLimited):
        src.search_jobs(task, QUERY)
    with pytest.raises(SourceRateLimited):
        src.search_jobs(task, QUERY)
    assert len(calls) == 1  # no hammering after a 429


def test_auth_required_is_surfaced_not_bypassed() -> None:
    src = make_source(lambda r: httpx.Response(403))
    with pytest.raises(SourceAuthRequired) as info:
        src.search_jobs(SearchTask(label="x", params={"board": "agoda"}), QUERY)
    assert "remove it" in info.value.user_action


def test_server_errors_are_retried_with_backoff() -> None:
    responses = [httpx.Response(503), httpx.Response(200, json={"jobs": [payload()]})]
    sleeps: list[float] = []
    src = make_source(lambda r: responses.pop(0), sleeps=sleeps)
    raws = src.search_jobs(SearchTask(label="x", params={"board": "agoda"}), QUERY)
    assert len(raws) == 1
    assert 2 in sleeps  # exponential backoff before the retry


def test_enrich_fetches_pay_transparency_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/boards/agoda/jobs/8063904"
        assert request.url.params["pay_transparency"] == "true"
        return httpx.Response(
            200,
            json=payload(
                pay_input_ranges=[
                    {"min_cents": 100000000, "max_cents": 150000000, "currency_type": "INR"}
                ]
            ),
        )

    src = make_source(handler)
    enriched = src.enrich(RawJob(source_job_id="agoda:8063904", payload=payload()))
    assert src.normalize_job(enriched).salary_max == 1500000.0
