"""Lever, Ashby, SmartRecruiters, Remotive and Adzuna adapters. HTTP is mocked: nothing here
touches the network."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2 as httpx
import pytest

from backend.core.normalize import normalize_salary
from backend.services.freshness import compute_freshness
from database.enums import Freshness, RemoteType
from integrations.job_sources.adzuna import AdzunaSource
from integrations.job_sources.ashby import AshbySource
from integrations.job_sources.base import (
    JobSource,
    RawJob,
    SearchQuery,
    SearchTask,
    SourceAuthRequired,
    SourceError,
    SourceRateLimited,
    parse_naive_as_earliest,
)
from integrations.job_sources.lever import LeverSource
from integrations.job_sources.remotive import RemotiveSource
from integrations.job_sources.smartrecruiters import SmartRecruitersSource

NOW = datetime.now(UTC)
QUERY = SearchQuery(terms=["Software Engineer"], locations=["Noida", "Gurugram"])
VOCAB = ["Python", "Java", "Kafka"]


def client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# --- payload builders -----------------------------------------------------


def lever_job(i: int = 1, **over: Any) -> dict[str, Any]:
    return {
        "id": f"lv-{i}",
        "text": "Software Engineer I",
        "categories": {"location": "Noida", "team": "Engineering", "commitment": "Full-time"},
        "country": "IN",
        "workplaceType": "onsite",
        "createdAt": ms(NOW - timedelta(hours=5)),
        "hostedUrl": f"https://jobs.lever.co/acme/lv-{i}",
        "applyUrl": f"https://jobs.lever.co/acme/lv-{i}/apply",
        "description": "<p>Build services in Python.</p>",
        "lists": [
            {"text": "Requirements", "content": "<li>0-2 years of experience</li><li>Kafka</li>"}
        ],
        **over,
    }


def ashby_job(**over: Any) -> dict[str, Any]:
    return {
        "id": "ab-1",
        "title": "Software Engineer",
        "location": "Gurugram",
        "secondaryLocations": [],
        "isRemote": False,
        "workplaceType": "OnSite",
        "isListed": True,
        "descriptionHtml": "<p>Python and Java. 1-2 years of experience.</p>",
        "publishedAt": (NOW - timedelta(hours=3)).isoformat(),
        "jobUrl": "https://jobs.ashbyhq.com/acme/ab-1",
        "applyUrl": "https://jobs.ashbyhq.com/acme/ab-1/application",
        "address": {"postalAddress": {"addressLocality": "Gurugram", "addressCountry": "India"}},
        **over,
    }


def sr_job(i: int = 1, **over: Any) -> dict[str, Any]:
    return {
        "id": f"{1000 + i}",
        "name": "Associate Software Engineer",
        "releasedDate": (NOW - timedelta(hours=10)).isoformat().replace("+00:00", "Z"),
        "location": {"city": "Noida", "region": "UP", "country": "in", "remote": False},
        "company": {"identifier": "Acme", "name": "Acme India"},
        "experienceLevel": {"id": "entry_level", "label": "Entry Level"},
        **over,
    }


def remotive_job(**over: Any) -> dict[str, Any]:
    return {
        "id": 77,
        "url": "https://remotive.com/remote-jobs/software-dev/se-77",
        "title": "Junior Software Engineer",
        "company_name": "Remote Co",
        "category": "Software Development",
        "candidate_required_location": "India",
        "publication_date": (NOW - timedelta(hours=6))
        .replace(tzinfo=None)
        .isoformat(timespec="seconds"),
        "salary": "₹8L - ₹12L",
        "description": "<p>Python backend.</p>",
        **over,
    }


def adzuna_job(**over: Any) -> dict[str, Any]:
    return {
        "id": "4001",
        "title": "Graduate Software Engineer",
        "company": {"display_name": "Acme Tech"},
        "location": {
            "display_name": "Noida, Uttar Pradesh",
            "area": ["India", "Uttar Pradesh", "Noida"],
        },
        "description": "Python. 0-1 years of experience.",
        "redirect_url": "https://www.adzuna.in/details/4001",
        "created": (NOW - timedelta(hours=20)).isoformat().replace("+00:00", "Z"),
        "salary_min": 600000,
        "salary_max": 900000,
        "salary_is_predicted": "1",
        **over,
    }


def lever(handler: Any, **kw: Any) -> LeverSource:
    return LeverSource(
        [{"slug": "acme", "name": "Acme"}],
        skills_vocabulary=VOCAB,
        user_agent="t",
        request_delay_seconds=0,
        client=client(handler),
        sleep=lambda s: None,
        **kw,
    )


def ashby(handler: Any) -> AshbySource:
    return AshbySource(
        [{"slug": "acme", "name": "Acme"}],
        skills_vocabulary=VOCAB,
        user_agent="t",
        request_delay_seconds=0,
        client=client(handler),
        sleep=lambda s: None,
    )


def smart(handler: Any, **kw: Any) -> SmartRecruitersSource:
    return SmartRecruitersSource(
        [{"slug": "Acme", "name": "Acme"}],
        skills_vocabulary=VOCAB,
        user_agent="t",
        request_delay_seconds=0,
        client=client(handler),
        sleep=lambda s: None,
        **kw,
    )


def remotive(handler: Any, **kw: Any) -> RemotiveSource:
    return RemotiveSource(
        ["software-dev"],
        skills_vocabulary=VOCAB,
        user_agent="t",
        request_delay_seconds=0,
        client=client(handler),
        sleep=lambda s: None,
        **kw,
    )


def adzuna(handler: Any) -> AdzunaSource:
    return AdzunaSource(
        app_id="test-app-id-1234",
        app_key="TESTKEY9876abcd",
        skills_vocabulary=VOCAB,
        user_agent="t",
        request_delay_seconds=0,
        client=client(handler),
        sleep=lambda s: None,
        results_per_page=2,
    )


def first_task(src: JobSource) -> SearchTask:
    return src.plan(QUERY)[0]


def fresh(posted: datetime | None) -> Freshness:
    return compute_freshness(posted, datetime.now(UTC), 48)


# --- successful discovery + normalization ------------------------------------------------------


def test_lever_discovery_and_normalization() -> None:
    src = lever(lambda r: httpx.Response(200, json=[lever_job()]))
    raws = src.search_jobs(first_task(src), QUERY)
    job = src.normalize_job(raws[0])
    assert (job.source, job.source_job_id, job.company) == ("lever", "acme:lv-1", "Acme")
    assert job.location == "Noida, India" and job.remote_type is RemoteType.ONSITE
    assert job.posted_at_kind == "created" and fresh(job.posted_at) is Freshness.FRESH
    assert job.experience_required and "0-2" in job.experience_required
    assert "Kafka" in job.skills and job.url.startswith("https://jobs.lever.co/")
    assert job.salary_min is None and job.salary_raw is None  # not disclosed: unknown


def test_ashby_discovery_and_normalization() -> None:
    src = ashby(lambda r: httpx.Response(200, json={"jobs": [ashby_job()]}))
    job = src.normalize_job(src.search_jobs(first_task(src), QUERY)[0])
    assert job.company == "Acme" and "Gurugram" in (job.location or "")
    assert job.posted_at_kind == "published" and fresh(job.posted_at) is Freshness.FRESH


def test_ashby_skips_unlisted_postings() -> None:
    src = ashby(lambda r: httpx.Response(200, json={"jobs": [ashby_job(isListed=False)]}))
    assert src.search_jobs(first_task(src), QUERY) == []


def test_smartrecruiters_discovery_filters_india_and_enriches() -> None:
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        if r.url.path.endswith("/postings"):
            return httpx.Response(200, json={"totalFound": 1, "content": [sr_job()]})
        return httpx.Response(
            200,
            json={
                "postingUrl": "https://jobs.smartrecruiters.com/Acme/1001",
                "jobAd": {
                    "sections": {
                        "jobDescription": {
                            "title": "Role",
                            "text": "<p>Python, 0-1 years of experience</p>",
                        }
                    }
                },
            },
        )

    src = smart(handler)
    raws = src.search_jobs(first_task(src), QUERY)
    assert seen[0].url.params["country"] == "in"  # server-side India filter
    job = src.normalize_job(src.enrich(raws[0]))
    assert job.company == "Acme India" and job.location == "Noida | UP, India"
    assert job.posted_at_kind == "released" and "Python" in job.skills
    assert job.url == "https://jobs.smartrecruiters.com/Acme/1001"
    assert job.salary_min is None  # this API never publishes pay


def test_remotive_discovery_keeps_link_back_and_raw_salary() -> None:
    src = remotive(lambda r: httpx.Response(200, json={"jobs": [remotive_job()]}))
    job = src.normalize_job(src.search_jobs(first_task(src), QUERY)[0])
    assert job.location == "Remote - India" and job.remote_type is RemoteType.REMOTE
    assert job.url.startswith("https://remotive.com/")
    assert job.salary_min is None and job.salary_raw == {"text": "₹8L - ₹12L"}  # never parsed
    assert job.posted_at_kind == "published_tz_unknown"


def test_adzuna_discards_predicted_salary_and_hides_key() -> None:
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json={"results": [adzuna_job()]})

    src = adzuna(handler)
    tasks = src.plan(SearchQuery(terms=["Software Engineer"], locations=["Noida", "Remote"]))
    assert [t.params["where"] for t in tasks] == ["Noida"]  # "Remote" is not a place
    job = src.normalize_job(src.search_jobs(tasks[0], QUERY)[0])
    assert seen[0].url.params["max_days_old"] == "2"
    assert job.salary_min is None and job.salary_max is None  # Adzuna's estimate, discarded
    assert job.salary_raw and job.salary_raw["salary_is_predicted"] == "1"
    assert job.location == "Noida, Uttar Pradesh, India"
    assert job.posted_at is None and job.posted_at_kind == "unknown"  # 'created' undocumented
    disclosed = src.normalize_job(
        RawJob(source_job_id="x", payload=adzuna_job(salary_is_predicted="0"))
    )
    # Adzuna documents no pay period for numeric figures, so even disclosed numbers stay
    # unknown (the raw values are kept); see tests/unit/test_adzuna.py for the full rules.
    assert disclosed.salary_min is None and disclosed.salary_raw


def test_adzuna_requires_the_users_own_key() -> None:
    with pytest.raises(ValueError, match="ADZUNA_APP_ID"):
        AdzunaSource(app_id=None, app_key=None, skills_vocabulary=[], user_agent="t")


# --- pagination -----------------------------------------------------


def test_lever_paginates_with_skip_and_limit() -> None:
    pages = {0: [lever_job(1), lever_job(2)], 2: [lever_job(3)]}
    src = lever(
        lambda r: httpx.Response(200, json=pages.get(int(r.url.params["skip"]), [])), page_size=2
    )
    assert [r.source_job_id for r in src.search_jobs(first_task(src), QUERY)] == [
        "acme:lv-1",
        "acme:lv-2",
        "acme:lv-3",
    ]


def test_smartrecruiters_paginates_until_total_found() -> None:
    offsets: list[int] = []

    def handler(r: httpx.Request) -> httpx.Response:
        offset = int(r.url.params["offset"])
        offsets.append(offset)
        return httpx.Response(
            200,
            json={"totalFound": 3, "content": [sr_job(offset), sr_job(offset + 1)][: 3 - offset]},
        )

    src = smart(handler, page_size=2)
    assert len(src.search_jobs(first_task(src), QUERY)) == 3 and offsets == [0, 2]


def test_adzuna_paginates() -> None:
    pages: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        pages.append(r.url.path.rsplit("/", 1)[-1])
        n = 2 if pages[-1] == "1" else 1
        return httpx.Response(
            200, json={"results": [adzuna_job(id=f"{pages[-1]}-{i}") for i in range(n)]}
        )

    src = adzuna(handler)
    assert len(src.search_jobs(src.plan(QUERY)[0], QUERY)) == 3 and pages == ["1", "2"]


# --- malformed responses and missing data -----------------------------------------------------


@pytest.mark.parametrize(
    ("build", "body"),
    [
        (lever, {"unexpected": "object"}),
        (ashby, ["not", "an", "object"]),
        (smart, {"content": "nope"}),
        (remotive, {"jobs": "nope"}),
        (adzuna, {"results": None}),
    ],
)
def test_malformed_payloads_are_source_errors(build: Any, body: Any) -> None:
    src = build(lambda r: httpx.Response(200, json=body))
    with pytest.raises(SourceError):
        src.search_jobs(first_task(src), QUERY)


@pytest.mark.parametrize("build", [lever, ashby, smart, remotive, adzuna])
def test_invalid_json_is_a_source_error(build: Any) -> None:
    src = build(lambda r: httpx.Response(200, content=b"<html>not json</html>"))
    with pytest.raises(SourceError, match="invalid JSON"):
        src.search_jobs(first_task(src), QUERY)


def test_malformed_posting_fails_normalization() -> None:
    src = lever(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(ValueError):
        src.normalize_job(RawJob(source_job_id="acme:x", payload=lever_job(text="  ")))


@pytest.mark.parametrize(
    ("build", "raw"),
    [
        (lever, lever_job(createdAt=None)),
        (lever, lever_job(createdAt="yesterday")),
        (ashby, ashby_job(publishedAt=None)),
        (smart, sr_job(releasedDate=None)),
        (remotive, remotive_job(publication_date="")),
        (adzuna, adzuna_job(created="not a date")),
    ],
)
def test_missing_timestamp_means_unknown_freshness(build: Any, raw: dict[str, Any]) -> None:
    src = build(lambda r: httpx.Response(200, json={}))
    job = src.normalize_job(
        RawJob(source_job_id="acme:1", payload={**raw, "_company": "acme", "_board": "acme"})
    )
    assert job.posted_at is None and job.posted_at_kind == "unknown"
    assert fresh(job.posted_at) is Freshness.UNKNOWN


# --- freshness -----------------------------------------------------


@pytest.mark.parametrize(
    ("hours", "expected"),
    [(1, Freshness.FRESH), (47, Freshness.FRESH), (49, Freshness.STALE), (400, Freshness.STALE)],
)
def test_fresh_and_stale_postings(hours: int, expected: Freshness) -> None:
    posted = NOW - timedelta(hours=hours)
    src = lever(lambda r: httpx.Response(200, json=[]))
    job = src.normalize_job(
        RawJob(
            source_job_id="acme:1", payload={**lever_job(createdAt=ms(posted)), "_company": "acme"}
        )
    )
    assert fresh(job.posted_at) is expected


def test_timezone_less_timestamps_never_look_fresher_than_they_are() -> None:
    naive = (NOW - timedelta(hours=40)).replace(tzinfo=None).isoformat(timespec="seconds")
    earliest = parse_naive_as_earliest(naive)
    assert earliest is not None and earliest <= NOW - timedelta(hours=40 + 13)
    # 40h old if written in UTC, but up to 54h old if written at UTC+14: not claimed fresh.
    assert fresh(earliest) is Freshness.STALE


def test_update_time_is_never_used_as_a_post_date() -> None:
    from integrations.job_sources.greenhouse import GreenhouseSource

    gh = GreenhouseSource(
        ["b"],
        skills_vocabulary=[],
        user_agent="t",
        client=client(lambda r: httpx.Response(200, json={})),
    )
    job = gh.normalize_job(
        RawJob(
            source_job_id="b:1",
            payload={
                "id": 1,
                "title": "Software Engineer",
                "absolute_url": "https://x.test/1",
                "location": {"name": "Noida"},
                "updated_at": NOW.isoformat(),
                "_board": "b",
            },
        )
    )
    assert job.posted_at is None and job.source_updated_at is not None
    assert fresh(job.posted_at) is Freshness.UNKNOWN  # updated today, but not "posted" today


# --- location -----------------------------------------------------


def test_locations_come_only_from_the_source() -> None:
    src = lever(lambda r: httpx.Response(200, json=[]))
    remote = src.normalize_job(
        RawJob(
            source_job_id="acme:1",
            payload={
                **lever_job(workplaceType="remote", categories={"location": "Remote"}),
                "_company": "acme",
            },
        )
    )
    assert remote.location == "Remote, India" and remote.remote_type is RemoteType.REMOTE
    no_country = src.normalize_job(
        RawJob(
            source_job_id="acme:2",
            payload={
                **lever_job(country=None, categories={"location": "Noida"}),
                "_company": "acme",
            },
        )
    )
    assert no_country.location == "Noida"  # no country invented


# --- salary -----------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((1_000_000, 1_500_000, "inr", "per-year-salary"), (1_000_000, 1_500_000, "INR", "YEAR")),
        ((80_000, 100_000, "INR", "per-month-salary"), (960_000, 1_200_000, "INR", "MONTH")),
        ((40, 60, "USD", "per-hour-wage"), (None, None, None, "HOUR")),  # would need assumed hours
        ((50_000, None, "USD", None), (None, None, None, None)),  # period unknown
        ((None, None, "INR", "YEAR"), (None, None, None, "YEAR")),
        ((1_000, 500, "INR", "YEAR"), (None, None, None, "YEAR")),  # inverted range
        ((500_000, 700_000, None, "YEAR"), (None, None, None, "YEAR")),  # no currency
    ],
)
def test_salary_normalization(args: tuple[Any, ...], expected: tuple[Any, ...]) -> None:
    assert normalize_salary(*args) == expected


def test_lever_monthly_salary_keeps_the_original_value() -> None:
    salary = {"min": 80000, "max": 100000, "currency": "INR", "interval": "per-month-salary"}
    src = lever(lambda r: httpx.Response(200, json=[]))
    job = src.normalize_job(
        RawJob(
            source_job_id="acme:1", payload={**lever_job(salaryRange=salary), "_company": "acme"}
        )
    )
    assert (job.salary_min, job.salary_max, job.salary_period) == (960000, 1200000, "MONTH")
    assert job.salary_raw == salary


def test_ashby_uses_only_one_explicit_salary_component() -> None:
    comp = {
        "compensationTierSummary": "₹12L – ₹18L",
        "summaryComponents": [
            {
                "compensationType": "Salary",
                "interval": "1 YEAR",
                "currencyCode": "INR",
                "minValue": 1200000,
                "maxValue": 1800000,
            },
            {
                "compensationType": "EquityPercentage",
                "interval": "NONE",
                "minValue": 0.1,
                "maxValue": 0.2,
            },
        ],
    }
    src = ashby(lambda r: httpx.Response(200, json={}))
    job = src.normalize_job(
        RawJob(source_job_id="acme:1", payload={**ashby_job(compensation=comp), "_board": "acme"})
    )
    assert (job.salary_min, job.salary_max, job.salary_currency) == (1200000, 1800000, "INR")
    two = {**comp, "summaryComponents": comp["summaryComponents"][:1] * 2}
    job = src.normalize_job(
        RawJob(source_job_id="acme:1", payload={**ashby_job(compensation=two), "_board": "acme"})
    )
    assert job.salary_min is None and job.salary_raw  # ambiguous tiers: unknown, raw kept


# --- failures -----------------------------------------------------


@pytest.mark.parametrize("build", [lever, ashby, smart, adzuna])
def test_api_failure_after_retries(build: Any) -> None:
    calls: list[int] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    src = build(handler)
    with pytest.raises(SourceError, match="503"):
        src.search_jobs(first_task(src), QUERY)
    assert len(calls) == 3  # bounded retries


@pytest.mark.parametrize("build", [lever, ashby, smart, remotive, adzuna])
def test_rate_limit_stops_the_source_for_the_run(build: Any) -> None:
    calls: list[int] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429)

    src = build(handler)
    with pytest.raises(SourceRateLimited):
        src.search_jobs(first_task(src), QUERY)
    with pytest.raises(SourceRateLimited):  # no second request this run
        src.search_jobs(first_task(src), QUERY)
    assert len(calls) == 1


@pytest.mark.parametrize("build", [lever, ashby, smart, remotive, adzuna])
def test_timeouts_become_source_errors(build: Any) -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=r)

    src = build(handler)
    with pytest.raises(SourceError, match="timed out"):
        src.search_jobs(first_task(src), QUERY)


@pytest.mark.parametrize("build", [lever, ashby, smart])
def test_private_boards_need_the_user(build: Any) -> None:
    src = build(lambda r: httpx.Response(403))
    with pytest.raises(SourceAuthRequired):
        src.search_jobs(first_task(src), QUERY)


@pytest.mark.parametrize("build", [lever, ashby, smart])
def test_missing_board_is_reported(build: Any) -> None:
    src = build(lambda r: httpx.Response(404))
    with pytest.raises(SourceError, match="does not exist"):
        src.search_jobs(first_task(src), QUERY)


def test_remotive_respects_its_request_budget() -> None:
    calls: list[int] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"jobs": []})

    src = RemotiveSource(
        ["a", "b", "c"],
        skills_vocabulary=[],
        user_agent="t",
        request_delay_seconds=0,
        max_requests=2,
        client=client(handler),
        sleep=lambda s: None,
    )
    tasks = src.plan(QUERY)
    src.search_jobs(tasks[0], QUERY)
    src.search_jobs(tasks[1], QUERY)
    with pytest.raises(SourceRateLimited, match="budget"):
        src.search_jobs(tasks[2], QUERY)
    assert len(calls) == 2


# --- company exclusion -----------------------------------------------------


@pytest.mark.parametrize(
    ("company", "excluded"),
    [
        ("Acme Labs.ai", True),
        ("Acme Labs AI", True),
        ("Acme Labs AI Pvt Ltd", True),
        ("ACME LABS AI PVT. LTD.", True),
        ("Acme Labs Sciences", False),
        ("Acme Labsmith", False),
        ("Acme", False),
    ],
)
def test_company_exclusion_matching(profile: Any, company: str, excluded: bool) -> None:
    from backend.services.shortlist import exclusion_check

    prefs = profile.preferences.model_copy(
        update={"excluded_companies": ["Acme Labs.ai", "Acme Labs AI", "Acme Labs AI Pvt Ltd"]}
    )
    assert (exclusion_check(company, "Software Engineer", prefs) is not None) is excluded


def test_every_source_describes_itself() -> None:
    for src in (
        lever(lambda r: httpx.Response(200)),
        ashby(lambda r: httpx.Response(200)),
        smart(lambda r: httpx.Response(200)),
        remotive(lambda r: httpx.Response(200)),
        adzuna(lambda r: httpx.Response(200)),
    ):
        info = src.describe()
        assert info["discovery_method"] and info["docs_url"].startswith("https://")
        assert info["timestamp_kind"] in (
            "published",
            "released",
            "created",
            "published_tz_unknown",
            "unknown",
        )


def test_empty_compensation_is_unknown_not_an_empty_record() -> None:
    src = ashby(lambda r: httpx.Response(200, json={}))
    empty = {"compensationTierSummary": None, "summaryComponents": []}
    job = src.normalize_job(
        RawJob(source_job_id="acme:1", payload={**ashby_job(compensation=empty), "_board": "acme"})
    )
    assert job.salary_raw is None and job.salary_min is None
