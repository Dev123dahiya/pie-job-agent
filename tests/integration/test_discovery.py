"""End-to-end discovery on the mock fixtures (no network)."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.services.discovery import DiscoveryService, create_discovery_run, location_matches
from backend.services.runs import RunConflictError, active_run, request_cancel
from database.base import utcnow
from database.enums import AgentActionStatus, AgentRunStatus, JobLogAction, JobPipelineState
from database.models import AgentAction, AgentRun, ApplicationEvent, Job
from integrations.job_sources.base import SearchQuery
from integrations.job_sources.config import JobSourcesConfig
from integrations.job_sources.mock import MockSource


@pytest.fixture
def query(sources_config: JobSourcesConfig) -> SearchQuery:
    return sources_config.build_query([], [])


def run_discovery(factory: sessionmaker[Session], source: MockSource, query: SearchQuery):  # type: ignore[no-untyped-def]
    with factory() as db:
        run = create_discovery_run(db, source, query, dev_mode=True)
    return DiscoveryService(factory).execute(run.id, source, query)


def jobs_by_mock_id(db: Session) -> dict[str, Job]:
    return {j.source_job_id: j for j in db.scalars(select(Job))}


def test_discovery_normalizes_filters_dedupes_and_counts(
    session_factory: sessionmaker[Session], mock_source: MockSource, query: SearchQuery, db: Session
) -> None:
    s = run_discovery(session_factory, mock_source, query)
    assert s.status is AgentRunStatus.COMPLETED
    assert (s.searches_total, s.searches_completed) == (2, 2)
    # 12 fixtures: 9 match roles+locations (sales role and Singapore excluded; 1 malformed)
    assert (s.jobs_found, s.jobs_new, s.jobs_duplicate, s.jobs_failed) == (9, 7, 2, 1)

    jobs = jobs_by_mock_id(db)
    assert set(jobs) == {
        "mock-001",
        "mock-002",
        "mock-003",
        "mock-004",
        "mock-007",
        "mock-008",
        "mock-012",
    }
    assert all(j.pipeline_state is JobPipelineState.DISCOVERED for j in jobs.values())

    strong = jobs["mock-001"]
    assert strong.company is not None and strong.company.name == "Nimbus Analytics Pvt. Ltd."
    assert (float(strong.salary_min), float(strong.salary_max), strong.salary_currency) == (
        1200000,
        1800000,
        "INR",
    )  # type: ignore[arg-type]
    assert strong.experience_required == "0-2 years"
    assert {"Python", "SQL", "FastAPI", "Django", "Docker"} <= set(strong.skills)
    assert strong.canonical_url == "https://jobs.example.invalid/nimbus/1001"
    assert strong.fingerprint and strong.discovery_run_id == s.run_id

    missing_salary = jobs["mock-003"]
    assert missing_salary.salary_min is None and missing_salary.salary_max is None
    assert missing_salary.salary_currency is None
    assert jobs["mock-004"].experience_required is None  # posting states no experience
    assert jobs["mock-012"].posted_at is None
    assert jobs["mock-008"].remote_type.value == "remote"

    events = db.scalars(
        select(ApplicationEvent).where(ApplicationEvent.event_type == "job_discovered")
    ).all()
    assert len(events) == 7


def test_repeat_discovery_creates_no_duplicates(
    session_factory: sessionmaker[Session], mock_source: MockSource, query: SearchQuery, db: Session
) -> None:
    run_discovery(session_factory, mock_source, query)
    second = run_discovery(session_factory, mock_source, query)
    assert (second.jobs_found, second.jobs_new, second.jobs_duplicate) == (9, 0, 9)
    assert db.scalar(select(func.count()).select_from(Job)) == 7
    # mock-001 seen: run 1 (new + its URL duplicate mock-005), run 2 (itself + mock-005)
    assert jobs_by_mock_id(db)["mock-001"].times_seen == 4


def test_duplicate_reasons_are_logged(
    session_factory: sessionmaker[Session], mock_source: MockSource, query: SearchQuery, db: Session
) -> None:
    s = run_discovery(session_factory, mock_source, query)
    dupes = db.scalars(
        select(AgentAction).where(
            AgentAction.run_id == s.run_id,
            AgentAction.tool_name == JobLogAction.JOB_DUPLICATE.value,
        )
    ).all()
    reasons = sorted(a.output["matched_on"] for a in dupes if a.output)
    assert reasons == ["canonical_url", "fingerprint"]


def test_action_log_covers_the_pipeline(
    session_factory: sessionmaker[Session], mock_source: MockSource, query: SearchQuery, db: Session
) -> None:
    s = run_discovery(session_factory, mock_source, query)
    actions = [
        a.tool_name
        for a in db.scalars(
            select(AgentAction).where(AgentAction.run_id == s.run_id).order_by(AgentAction.sequence)
        )
    ]
    assert actions[0] == "DISCOVERY_STARTED" and actions[-1] == "DISCOVERY_COMPLETED"
    for code in (
        "JOB_DISCOVERED",
        "JOB_NORMALIZED",
        "JOB_DUPLICATE",
        "JOB_FAILED",
        "SEARCH_COMPLETED",
    ):
        assert code in actions, code
    failed = db.scalar(select(AgentAction).where(AgentAction.tool_name == "JOB_FAILED"))
    assert (
        failed is not None
        and failed.status is AgentActionStatus.FAILED
        and "title" in (failed.error or "")
    )


def test_one_failing_search_does_not_stop_the_run(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    query: SearchQuery,
    db: Session,
) -> None:
    source = MockSource(skills_vocabulary=sources_config.skills_vocabulary, fail_boards={"A"})
    s = run_discovery(session_factory, source, query)
    assert s.status is AgentRunStatus.COMPLETED
    assert s.searches_completed == 2 and s.errors == 1
    assert s.jobs_new == 5  # board B still processed (mock-001/003 now first seen via B)
    run = db.get(AgentRun, s.run_id)
    assert run is not None and "configured to fail" in run.errors[0]["error"]


def test_all_searches_failing_marks_run_failed(
    session_factory: sessionmaker[Session], sources_config: JobSourcesConfig, query: SearchQuery
) -> None:
    source = MockSource(skills_vocabulary=sources_config.skills_vocabulary, fail_boards={"A", "B"})
    assert run_discovery(session_factory, source, query).status is AgentRunStatus.FAILED


def test_cancellation_stops_before_next_search(
    session_factory: sessionmaker[Session], mock_source: MockSource, query: SearchQuery, db: Session
) -> None:
    with session_factory() as s:
        run = create_discovery_run(s, mock_source, query, dev_mode=True)
        request_cancel(s, run.id)
    summary = DiscoveryService(session_factory).execute(run.id, mock_source, query)
    assert summary.status is AgentRunStatus.STOPPED
    assert summary.searches_completed == 0
    assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_only_one_active_run_and_dead_runs_are_reaped(
    session_factory: sessionmaker[Session], mock_source: MockSource, query: SearchQuery, db: Session
) -> None:
    run = create_discovery_run(db, mock_source, query, dev_mode=True)
    with pytest.raises(RunConflictError):
        create_discovery_run(db, mock_source, query, dev_mode=True)
    run.heartbeat_at = utcnow() - timedelta(hours=1)  # worker died
    db.commit()
    assert active_run(db) is None
    db.refresh(run)
    assert run.status is AgentRunStatus.FAILED and "stopped responding" in run.errors[-1]["error"]


def test_location_matching_uses_configured_patterns(query: SearchQuery) -> None:
    assert location_matches("Gurgaon, Haryana", query) == "Gurugram"
    assert location_matches("New Delhi, India", query) == "Delhi NCR"
    assert location_matches("Remote - India", query) == "Remote"
    assert location_matches("Remote - Bangalore, India", query) == "Remote"
    assert location_matches("Remote", query) is None  # bare "Remote" is usually US-only
    assert location_matches("Remote - US", query) is None
    assert location_matches(None, query) is None


def test_location_scope_is_india_wide_with_ncr_first(query: SearchQuery) -> None:
    """Anywhere in India plus remote-India is in scope; NCR stays included (and is named
    first); places outside India are still rejected."""
    assert query.locations == ["Gurugram", "Noida", "Delhi NCR", "Remote", "India"]
    for ncr, name in [
        ("Gurugram, Haryana, India", "Gurugram"),
        ("Noida, Uttar Pradesh", "Noida"),
        ("Faridabad", "Delhi NCR"),
        ("New Delhi", "Delhi NCR"),
    ]:
        assert location_matches(ncr, query) == name
    for place in [
        "Bengaluru, India",
        "Bangalore",
        "Hyderabad, Telangana",
        "Pune",
        "Chennai",
        "Mumbai, Maharashtra",
        "Kolkata",
        "Kochi, Kerala",
        "India",
    ]:
        assert location_matches(place, query) == "India", place
    assert location_matches("India (Remote)", query) == "Remote"
    for outside in [
        "Singapore",
        "London, UK",
        "San Francisco, CA",
        "Indianapolis, Indiana",
        "Indian Ocean Territory",
        "Hyderabad, Sindh, Pakistan",
        "Remote - EMEA",
    ]:
        assert location_matches(outside, query) is None, outside


def test_preferred_locations_keep_ncr_first_and_add_india(sources_config: JobSourcesConfig) -> None:
    """The shipped search config (kept in sync with the profile by the Settings page). Tests
    never read the user's real profile."""
    locations = sources_config.search.locations
    assert locations[:3] == ["Gurugram", "Noida", "Delhi NCR"] and "India" in locations
