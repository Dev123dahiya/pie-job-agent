"""Adzuna adapter: client, timestamps, salary, secrets. HTTP is mocked; no network."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2 as httpx
import pytest

from backend.services.freshness import compute_freshness
from database.enums import Freshness
from integrations.job_sources.adzuna import AdzunaSource, adzuna_salary
from integrations.job_sources.base import (
    RawJob,
    SearchQuery,
    SourceAuthRequired,
    SourceError,
    SourceRateLimited,
    SourceTimeout,
)

APP_ID, APP_KEY = "a1b2c3d4", "0123456789abcdef0123456789abcdef"
NOW = datetime.now(UTC)
QUERY = SearchQuery(
    terms=["Software Engineer", "Data Engineer"],
    locations=["Gurugram", "Noida", "Delhi NCR", "Remote"],
)


def record(**over: Any) -> dict[str, Any]:
    return {
        "id": "5001",
        "title": "Graduate Software Engineer",
        "company": {"display_name": "Acme Tech"},
        "location": {"display_name": "Gurgaon, Haryana", "area": ["India", "Haryana", "Gurgaon"]},
        "description": "Python. 0-1 years of experience.",
        "redirect_url": f"https://www.adzuna.in/land/ad/5001?se=x&utm_source={APP_ID}&v=1",
        "created": (NOW - timedelta(hours=10)).isoformat().replace("+00:00", "Z"),
        "category": {"label": "IT Jobs", "tag": "it-jobs"},
        **over,
    }


def source(handler: Any, **kw: Any) -> AdzunaSource:
    return AdzunaSource(
        app_id=APP_ID,
        app_key=APP_KEY,
        skills_vocabulary=["Python"],
        user_agent="t",
        request_delay_seconds=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda s: None,
        results_per_page=kw.pop("results_per_page", 2),
        **kw,
    )


def ok(*records: dict[str, Any]) -> Any:
    return lambda r: httpx.Response(200, json={"count": len(records), "results": list(records)})


def norm(src: AdzunaSource, rec: dict[str, Any]) -> Any:
    return src.normalize_job(RawJob(source_job_id=str(rec.get("id")), payload=rec))


def fresh(posted: datetime | None) -> Freshness:
    return compute_freshness(posted, datetime.now(UTC), 48)


# --- client ----------------------------------------


def test_plan_searches_every_role_in_every_place_but_not_remote() -> None:
    src = source(ok(), location_queries={"Delhi NCR": "Delhi"})
    tasks = src.plan(QUERY)
    assert len(tasks) == 2 * 3
    assert {t.params["where"] for t in tasks} == {"Gurugram", "Noida", "Delhi"}
    assert {t.params["what"] for t in tasks} == {"Software Engineer", "Data Engineer"}


def test_successful_response_and_request_parameters() -> None:
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        return httpx.Response(200, json={"results": [record()]})

    src = source(handler)
    raws = src.search_jobs(src.plan(QUERY)[0], QUERY)
    assert [r.source_job_id for r in raws] == ["5001"] and src.last_pages == 1
    params = seen[0].url.params
    assert seen[0].url.path == "/v1/api/jobs/in/search/1"
    assert params["what"] == "Software Engineer" and params["where"] == "Gurugram"
    assert params["max_days_old"] == "2" and params["content-type"] == "application/json"
    assert params["app_id"] == APP_ID  # sent to Adzuna only, as the API requires


def test_pagination_stops_on_a_short_page() -> None:
    pages: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        pages.append(r.url.path.rsplit("/", 1)[-1])
        n = 2 if pages[-1] == "1" else 1
        return httpx.Response(
            200, json={"results": [record(id=f"{pages[-1]}-{i}") for i in range(n)]}
        )

    src = source(handler, max_pages=5)
    assert len(src.search_jobs(src.plan(QUERY)[0], QUERY)) == 3
    assert pages == ["1", "2"] and src.last_pages == 2


def test_pagination_is_capped() -> None:
    src = source(lambda r: httpx.Response(200, json={"results": [record(), record()]}), max_pages=2)
    src.search_jobs(src.plan(QUERY)[0], QUERY)
    assert src.last_pages == 2


def test_timeout() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=r)

    src = source(handler)
    with pytest.raises(SourceTimeout):
        src.search_jobs(src.plan(QUERY)[0], QUERY)


@pytest.mark.parametrize(("status", "error"), [(500, SourceError), (400, SourceError)])
def test_http_errors(status: int, error: type[Exception]) -> None:
    src = source(lambda r: httpx.Response(status))
    with pytest.raises(error):
        src.search_jobs(src.plan(QUERY)[0], QUERY)


def test_bad_credentials_are_reported_not_bypassed() -> None:
    src = source(lambda r: httpx.Response(401))
    with pytest.raises(SourceAuthRequired):
        src.search_jobs(src.plan(QUERY)[0], QUERY)


def test_rate_limit_stops_adzuna_for_the_run() -> None:
    calls: list[int] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429)

    src = source(handler)
    tasks = src.plan(QUERY)
    for task in tasks[:2]:
        with pytest.raises(SourceRateLimited):
            src.search_jobs(task, QUERY)
    assert len(calls) == 1


def test_request_budget_keeps_within_the_daily_terms() -> None:
    src = source(ok(record()), max_requests=3, results_per_page=1, max_pages=1)
    tasks = src.plan(QUERY)
    for task in tasks[:3]:
        src.search_jobs(task, QUERY)
    with pytest.raises(SourceRateLimited, match="budget"):
        src.search_jobs(tasks[3], QUERY)


def test_malformed_json() -> None:
    src = source(lambda r: httpx.Response(200, content=b"<html>"))
    with pytest.raises(SourceError, match="invalid JSON"):
        src.search_jobs(src.plan(QUERY)[0], QUERY)


@pytest.mark.parametrize("body", [{"results": None}, {"oops": []}, ["a"]])
def test_malformed_payload(body: Any) -> None:
    src = source(lambda r: httpx.Response(200, json=body))
    with pytest.raises(SourceError, match="unexpected payload"):
        src.search_jobs(src.plan(QUERY)[0], QUERY)


def test_malformed_records_are_kept_so_they_are_counted_then_fail_normalization() -> None:
    src = source(ok(record(), {"title": "no id"}, "not-a-dict"), results_per_page=5)  # type: ignore[arg-type]
    raws = src.search_jobs(src.plan(QUERY)[0], QUERY)
    assert len(raws) == 3
    with pytest.raises(ValueError, match="no id"):
        src.normalize_job(raws[1])
    with pytest.raises(ValueError):
        norm(src, record(title="  "))


def test_missing_credentials_are_a_clear_configuration_error() -> None:
    for app_id, app_key in ((None, None), ("", APP_KEY), (APP_ID, None)):
        with pytest.raises(ValueError, match="ADZUNA_APP_ID and ADZUNA_APP_KEY"):
            AdzunaSource(app_id=app_id, app_key=app_key, skills_vocabulary=[], user_agent="t")


def test_credentials_never_leak_into_errors_or_job_data() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {r.url}", request=r)  # URL includes the key

    src = source(handler)
    with pytest.raises(SourceError) as err:
        src.search_jobs(src.plan(QUERY)[0], QUERY)
    assert APP_ID not in str(err.value) and APP_KEY not in str(err.value)
    job = norm(source(ok()), record())
    dumped = job.model_dump_json()
    assert APP_ID not in dumped and APP_KEY not in dumped
    assert job.url == "https://www.adzuna.in/details/5001"  # never the tracking redirect


def test_registry_reads_credentials_only_from_settings(sources_config: Any) -> None:
    from backend.core.config import Settings
    from integrations.job_sources.registry import build_source

    with pytest.raises(ValueError, match="ADZUNA_APP_ID"):
        build_source("adzuna", sources_config, Settings(adzuna_app_id=None, adzuna_app_key=None))
    src = build_source(
        "adzuna", sources_config, Settings(adzuna_app_id=APP_ID, adzuna_app_key=APP_KEY)
    )
    assert isinstance(src, AdzunaSource) and src.created_is_publication is False
    src.close()


# --- timestamps ----------------------------------------


def test_created_is_not_a_post_date_by_default() -> None:
    job = norm(source(ok()), record())
    assert job.posted_at is None and job.posted_at_kind == "unknown"
    assert fresh(job.posted_at) is Freshness.UNKNOWN
    assert job.raw and job.raw["adzuna_created"]  # kept as metadata only


def test_valid_timezone_aware_timestamp_when_opted_in() -> None:
    job = norm(source(ok(), created_is_publication=True), record())
    assert job.posted_at_kind == "created" and fresh(job.posted_at) is Freshness.FRESH
    stale = norm(
        source(ok(), created_is_publication=True),
        record(created=(NOW - timedelta(hours=60)).isoformat()),
    )
    assert fresh(stale.posted_at) is Freshness.STALE


def test_timezone_less_timestamp_is_never_made_fresher() -> None:
    naive = (NOW - timedelta(hours=40)).replace(tzinfo=None).isoformat(timespec="seconds")
    job = norm(source(ok(), created_is_publication=True), record(created=naive))
    assert job.posted_at_kind == "published_tz_unknown"
    assert fresh(job.posted_at) is Freshness.STALE  # 40h if UTC, up to 54h otherwise


@pytest.mark.parametrize("value", [None, "", "yesterday", "2026-13-45T99:00:00Z", 1727500000])
def test_missing_or_malformed_timestamps_are_unknown(value: Any) -> None:
    for opt_in in (False, True):
        job = norm(source(ok(), created_is_publication=opt_in), record(created=value))
        assert job.posted_at is None and job.posted_at_kind == "unknown"


def test_an_update_timestamp_is_never_a_post_date() -> None:
    rec = record(created=None, updated=NOW.isoformat(), last_modified=NOW.isoformat())
    for opt_in in (False, True):
        job = norm(source(ok(), created_is_publication=opt_in), rec)
        assert job.posted_at is None and fresh(job.posted_at) is Freshness.UNKNOWN


# --- salary ----------------------------------------


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        # explicit yearly text -> used as is
        (
            {
                "salary_min": "₹8,00,000/year",
                "salary_max": "₹12,00,000/year",
                "salary_is_predicted": "0",
            },
            (800000, 1200000, "INR", "YEAR"),
        ),
        # explicit monthly text -> x12
        (
            {
                "salary_min": "₹80,000/month",
                "salary_max": "₹1,00,000/month",
                "salary_is_predicted": "0",
            },
            (960000, 1200000, "INR", "MONTH"),
        ),
        # hourly -> unknown
        (
            {"salary_min": "₹500/hour", "salary_max": "₹700/hour", "salary_is_predicted": "0"},
            (None, None, None, "HOUR"),
        ),
        # free text -> unknown
        (
            {"salary_min": "Competitive salary", "salary_is_predicted": "0"},
            (None, None, None, None),
        ),
        ({"salary_min": "Salary negotiable", "salary_is_predicted": "0"}, (None, None, None, None)),
        # numbers without a documented period -> unknown
        (
            {"salary_min": 800000, "salary_max": 1200000, "salary_is_predicted": "0"},
            (None, None, None, None),
        ),
        # Adzuna's own estimate -> unknown, even with a stated period
        ({"salary_min": "₹8,00,000/year", "salary_is_predicted": "1"}, (None, None, None, None)),
        ({"salary_min": 900000, "salary_is_predicted": 1}, (None, None, None, None)),
        # no flag at all: cannot tell an estimate from disclosed pay -> unknown
        ({"salary_min": "₹8,00,000/year"}, (None, None, None, None)),
        # mixed periods -> unknown
        (
            {
                "salary_min": "₹80,000/month",
                "salary_max": "₹12,00,000/year",
                "salary_is_predicted": "0",
            },
            (None, None, None, None),
        ),
    ],
)
def test_salary_rules(fields: dict[str, Any], expected: tuple[Any, ...]) -> None:
    low, high, currency, period, raw = adzuna_salary(fields)
    assert (low, high, currency, period) == expected
    assert raw == fields  # the original value is always preserved


def test_missing_salary_is_unknown_with_no_raw_record() -> None:
    assert adzuna_salary({}) == (None, None, None, None, None)
    assert adzuna_salary({"salary_is_predicted": "0"}) == (None, None, None, None, None)
    job = norm(source(ok()), record())
    assert job.salary_min is None and job.salary_max is None and job.salary_raw is None


def test_normalized_job_carries_salary_provenance() -> None:
    rec = record(salary_min="₹80,000/month", salary_is_predicted="0")
    job = norm(source(ok()), rec)
    assert (job.salary_min, job.salary_period, job.salary_currency) == (960000, "MONTH", "INR")
    assert job.salary_raw == {"salary_min": "₹80,000/month", "salary_is_predicted": "0"}


# --- normalization ----------------------------------------


def test_normalization_maps_only_what_adzuna_states() -> None:
    job = norm(source(ok()), record())
    assert job.source == "adzuna" and job.source_job_id == "5001"
    assert job.company == "Acme Tech" and job.title == "Graduate Software Engineer"
    assert job.location == "Gurgaon, Haryana, India"
    assert job.experience_required and "0-1" in job.experience_required
    assert "Python" in job.skills
    no_country = norm(source(ok()), record(location={"display_name": "Gurgaon"}))
    assert no_country.location == "Gurgaon"  # no country invented
