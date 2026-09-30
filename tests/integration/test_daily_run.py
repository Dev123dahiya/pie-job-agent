"""Phase 5.5: the daily discovery + analysis run.

Offline: the mock source (fixtures) and the deterministic heuristic analyzer; the SHIPPED
config/shortlist_rules.yaml (FRESH eligible; STALE and UNKNOWN rejected)."""

import time as systime
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from agent.analysis.analyzers import AnalyzerUnavailableError, HeuristicAnalyzer
from backend.api import deps
from backend.core.config import get_settings
from backend.core.profile import UserProfile
from backend.services.analysis import AnalysisContext, analyze_single
from backend.services.daily import (
    DailyPlan,
    DailyRunService,
    _Heartbeat,
    create_daily_run,
    run_daily,
)
from backend.services.freshness import compute_freshness
from backend.services.runs import RunConflictError
from backend.services.shortlist import load_shortlist_rules
from database.base import utcnow
from database.enums import AgentRunStatus, Freshness, JobLogAction, JobPipelineState, RunType
from database.models import (
    AgentAction,
    AgentRun,
    Application,
    BrowserSession,
    Job,
    JobAnalysis,
    SubmissionAttempt,
)
from integrations.job_sources.config import JobSourcesConfig
from integrations.job_sources.mock import MockSource

S = JobPipelineState


def fixture(job_id: str, hours: float | None, **extra: Any) -> dict[str, Any]:
    return {
        "id": job_id,
        "board": "A",
        "company": extra.pop("company", f"Company {job_id}"),
        "title": extra.pop("title", "Backend Software Engineer"),
        "location": "Noida, India",
        "posted_hours_ago": hours,
        "url": f"https://jobs.example.invalid/{job_id}",
        "description_html": "<p>0-1 years of experience. Python, SQL, FastAPI.</p>",
        **extra,
    }


@pytest.fixture
def make_plan(profile: UserProfile, sources_config: JobSourcesConfig) -> Callable[..., DailyPlan]:
    def make(
        fixtures: list[dict[str, Any]] | None = None,
        *,
        sources: list[str] | None = None,
        analyzer: Callable[[], Any] | None = None,
    ) -> DailyPlan:
        def build(name: str) -> MockSource:
            if name != "mock":
                raise ValueError(f"Source {name!r} is not configured")
            return MockSource(skills_vocabulary=sources_config.skills_vocabulary, fixtures=fixtures)

        prefs = profile.preferences
        heuristic = HeuristicAnalyzer(
            sources_config.search.location_patterns, sources_config.search.term_aliases
        )
        return DailyPlan(
            sources=sources or ["mock"],
            build_source=build,
            query=sources_config.build_query(
                [],
                [],
                excluded_companies=prefs.excluded_companies,
                max_age_hours=prefs.maximum_application_age_hours,
            ),
            analyzer=analyzer or (lambda: heuristic),
            profile=profile,
            rules=load_shortlist_rules(get_settings().shortlist_rules_path),  # shipped rules
            dev_mode=True,
            analysis_limit=50,
        )

    return make


def safety(db: Session) -> tuple[int, int, int]:
    return (
        db.scalar(select(func.count()).select_from(Application)) or 0,
        db.scalar(select(func.count()).select_from(BrowserSession)) or 0,
        db.scalar(select(func.count()).select_from(SubmissionAttempt)) or 0,
    )


def job(db: Session, source_job_id: str) -> Job:
    found = db.scalar(select(Job).where(Job.source_job_id == source_job_id))
    assert found is not None
    db.refresh(found)
    return found


def analyses(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(JobAnalysis)) or 0


# --- the daily run ------------------------------------------------------------------------


def test_daily_run_discovers_analyzes_fresh_jobs_and_reports(
    session_factory: sessionmaker[Session], make_plan: Callable[..., DailyPlan]
) -> None:
    run_id, report = run_daily(session_factory, make_plan(), "manual")

    with session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run is not None
        assert run.run_type is RunType.DAILY and run.status is AgentRunStatus.COMPLETED
        jobs = db.scalars(select(Job)).all()
        now = utcnow()
        fresh = [j for j in jobs if compute_freshness(j.posted_at, now, 48) is Freshness.FRESH]
        not_fresh = [j for j in jobs if j not in fresh]

        c = report["counts"]
        assert c["new"] == len(jobs) > 0
        assert c["discovered"] == c["new"] + c["duplicates"]
        assert c["fresh"] + c["stale"] + c["unknown"] == c["discovered"]
        assert c["stale"] >= 1 and c["unknown"] >= 1  # mock-007 (400h) and mock-012 (no date)
        # Only FRESH jobs go to the analyzer; STALE / UNKNOWN are ineligible and left alone.
        assert c["analyzed"] == len(fresh) == analyses(db)
        assert all(not j.analyses and j.pipeline_state is S.DISCOVERED for j in not_fresh)
        # The shipped shortlist rules decide: the strong fresh match is shortlisted.
        shortlisted = {j["title"] for j in report["newly_shortlisted"]}
        assert "Backend Software Engineer" in shortlisted
        assert c["shortlisted"] == len(report["newly_shortlisted"])
        assert job(db, "mock-012").pipeline_state is S.DISCOVERED  # UNKNOWN: never shortlisted
        assert job(db, "mock-007").pipeline_state is S.DISCOVERED  # STALE: never shortlisted
        # READY_TO_APPLY needs a draft + tailored resume, which a daily run never creates.
        assert c["ready_to_apply"] == 0 and report["newly_ready_to_apply"] == []
        assert c["failures"] == 0 and report["failures"] == []

        # History: one parent run, one discovery and one analysis child, a final report.
        children = db.scalars(select(AgentRun).where(AgentRun.id != run_id)).all()
        assert sorted(r.run_type.value for r in children) == ["analysis", "discovery"]
        done = db.scalar(
            select(AgentAction).where(
                AgentAction.run_id == run_id,
                AgentAction.tool_name == JobLogAction.DAILY_RUN_COMPLETED.value,
            )
        )
        assert done is not None and done.output["counts"] == c
        assert run.summary and "newly shortlisted" in run.summary
        assert run.jobs_new == c["new"] and run.jobs_analyzed == c["analyzed"]
        assert run.ended_at is not None and run.started_at <= run.ended_at
        assert safety(db) == (0, 0, 0)
        assert report["safety"]["after"] == {
            "applications": 0,
            "browser_sessions": 0,
            "submission_attempts": 0,
        }


def test_repeated_runs_create_no_duplicate_jobs_or_analyses(
    session_factory: sessionmaker[Session], make_plan: Callable[..., DailyPlan]
) -> None:
    _, first = run_daily(session_factory, make_plan(), "scheduled")
    with session_factory() as db:
        jobs_before, analyses_before = (
            db.scalar(select(func.count()).select_from(Job)),
            analyses(db),
        )

    _, second = run_daily(session_factory, make_plan(), "scheduled")
    _, third = run_daily(session_factory, make_plan(), "manual")

    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == jobs_before
        assert analyses(db) == analyses_before  # nothing re-analyzed
    for again in (second, third):
        c = again["counts"]
        assert c["new"] == 0
        assert c["duplicates"] == c["discovered"] == first["counts"]["discovered"]
        assert c["analyzed"] == 0 and c["shortlisted"] == 0
        assert again["newly_shortlisted"] == [] and again["analysis"]["status"] == "SKIPPED"


def test_stale_job_never_becomes_eligible_by_rediscovery(
    session_factory: sessionmaker[Session], make_plan: Callable[..., DailyPlan]
) -> None:
    run_daily(session_factory, make_plan([fixture("old-1", 100)]), "scheduled")
    with session_factory() as db:
        posted = job(db, "old-1").posted_at

    # Rediscovered, and the feed now claims a recent date for the same posting id.
    _, report = run_daily(session_factory, make_plan([fixture("old-1", 1)]), "scheduled")
    with session_factory() as db:
        stored = job(db, "old-1")
        assert stored.posted_at == posted  # an existing post date is never replaced
        assert compute_freshness(stored.posted_at, utcnow(), 48) is Freshness.STALE
        assert stored.pipeline_state is S.DISCOVERED and not stored.analyses
    assert report["counts"]["analyzed"] == 0 and report["counts"]["duplicates"] == 1


def test_unknown_job_is_reconsidered_once_its_source_publishes_a_date(
    session_factory: sessionmaker[Session],
    make_plan: Callable[..., DailyPlan],
    profile: UserProfile,
    sources_config: JobSourcesConfig,
) -> None:
    run_daily(session_factory, make_plan([fixture("u-1", None), fixture("u-2", None)]), "manual")
    with session_factory() as db:
        assert job(db, "u-1").posted_at is None and not job(db, "u-1").analyses
        # u-2 had been analyzed earlier (e.g. by the manual batch): UNKNOWN -> rejected.
        heuristic = HeuristicAnalyzer(
            sources_config.search.location_patterns, sources_config.search.term_aliases
        )
        rules = load_shortlist_rules(get_settings().shortlist_rules_path)
        first = analyze_single(
            db,
            job(db, "u-2"),
            AnalysisContext(analyzer=heuristic, profile=profile, rules=rules),
            dev_mode=True,
        )
        assert first is not None and first.freshness is Freshness.UNKNOWN
        assert job(db, "u-2").pipeline_state is S.REJECTED_BY_AGENT

    # The same postings, now with a publication time from their own source.
    _, report = run_daily(
        session_factory, make_plan([fixture("u-1", 3), fixture("u-2", 3)]), "scheduled"
    )
    with session_factory() as db:
        for sid in ("u-1", "u-2"):
            stored = job(db, sid)
            assert stored.posted_at is not None and stored.posted_at_kind == "published"
            latest = max(stored.analyses, key=lambda a: a.created_at)
            assert latest.freshness is Freshness.FRESH
        learned = db.scalar(
            select(func.count())
            .select_from(AgentAction)
            .where(AgentAction.tool_name == JobLogAction.JOB_POST_DATE_LEARNED.value)
        )
        assert learned == 2
    assert report["counts"]["analyzed"] == 2
    assert report["per_source"]["mock"]["post_dates_learned"] == 2


def test_learning_a_post_date_needs_the_same_source(
    session_factory: sessionmaker[Session], make_plan: Callable[..., DailyPlan]
) -> None:
    """A different posting (new id, new URL) that merely looks the same never lends its date."""
    run_daily(session_factory, make_plan([fixture("x-1", None)]), "manual")
    twin = fixture("x-2", 2, url="https://jobs.example.invalid/other")  # same company/title
    _, report = run_daily(session_factory, make_plan([twin]), "manual")
    with session_factory() as db:
        assert job(db, "x-1").posted_at is None
    assert report["per_source"]["mock"]["post_dates_learned"] == 0


# --- failures and recovery -------------------------------------------------------------------


def test_a_failing_source_is_recorded_and_the_rest_still_runs(
    session_factory: sessionmaker[Session], make_plan: Callable[..., DailyPlan]
) -> None:
    plan = make_plan(sources=["broken", "mock"])
    run_id, report = run_daily(session_factory, plan, "scheduled")
    assert report["per_source"]["broken"]["status"] == "FAILED"
    assert "not configured" in report["per_source"]["broken"]["error"]
    assert report["per_source"]["mock"]["status"] == "COMPLETED"
    assert report["counts"]["failures"] == 1 and report["failures"][0]["step"] == "discover:broken"
    assert report["counts"]["analyzed"] > 0
    with session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run is not None and run.status is AgentRunStatus.COMPLETED
        assert "failures 1" in (run.summary or "")


def test_analyzer_outage_leaves_jobs_for_the_next_run(
    session_factory: sessionmaker[Session], make_plan: Callable[..., DailyPlan]
) -> None:
    def unavailable() -> Any:
        raise AnalyzerUnavailableError("Claude CLI is not installed")

    _, report = run_daily(session_factory, make_plan(analyzer=unavailable), "scheduled")
    assert report["analysis"]["status"] == "FAILED" and report["counts"]["analyzed"] == 0
    assert report["counts"]["failures"] == 1
    with session_factory() as db:
        assert analyses(db) == 0

    _, retry = run_daily(session_factory, make_plan(), "scheduled")  # analyzer is back
    assert retry["counts"]["new"] == 0 and retry["counts"]["analyzed"] > 0


def test_every_step_failing_marks_the_run_failed(
    session_factory: sessionmaker[Session], make_plan: Callable[..., DailyPlan]
) -> None:
    run_id, report = run_daily(session_factory, make_plan(sources=["broken"]), "scheduled")
    with session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run is not None and run.status is AgentRunStatus.FAILED
    assert report["analysis"]["status"] == "SKIPPED"


def test_a_crashed_daily_run_does_not_block_the_next_one(
    session_factory: sessionmaker[Session], make_plan: Callable[..., DailyPlan]
) -> None:
    with session_factory() as db:
        dead = create_daily_run(db, make_plan(), "scheduled")
        dead.heartbeat_at = utcnow() - timedelta(minutes=30)  # the worker died mid-run
        db.commit()
        dead_id = dead.id
    run_id, report = run_daily(session_factory, make_plan(), "scheduled")
    with session_factory() as db:
        old = db.get(AgentRun, dead_id)
        assert old is not None and old.status is AgentRunStatus.FAILED
        new = db.get(AgentRun, run_id)
        assert new is not None and new.status is AgentRunStatus.COMPLETED
    assert report["counts"]["new"] > 0


def test_unexpected_crash_is_recorded_as_failed(
    session_factory: sessionmaker[Session],
    make_plan: Callable[..., DailyPlan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr("backend.services.daily.DailyRunService._collect_outcomes", boom)
    run_id, report = run_daily(session_factory, make_plan(), "scheduled")
    with session_factory() as db:
        run = db.get(AgentRun, run_id)
        assert run is not None and run.status is AgentRunStatus.FAILED
        assert run.ended_at is not None and run.errors
    assert report["failures"][-1]["step"] == "daily"


def test_only_one_run_at_a_time(
    session_factory: sessionmaker[Session], make_plan: Callable[..., DailyPlan]
) -> None:
    with session_factory() as db:
        create_daily_run(db, make_plan(), "scheduled")  # live (fresh heartbeat)
    with pytest.raises(RunConflictError):
        run_daily(session_factory, make_plan(), "manual")


def test_heartbeat_keeps_a_long_run_alive(
    session_factory: sessionmaker[Session], make_plan: Callable[..., DailyPlan]
) -> None:
    with session_factory() as db:
        run = create_daily_run(db, make_plan(), "scheduled")
        run.heartbeat_at = utcnow() - timedelta(minutes=9)
        db.commit()
        before = run.heartbeat_at
        with _Heartbeat(session_factory, run.id, 0.02):
            systime.sleep(0.2)
        db.refresh(run)
        assert run.heartbeat_at is not None and before is not None and run.heartbeat_at > before


# --- safety ---------------------------------------------------------------------------------


def test_daily_run_never_creates_applications_or_opens_a_browser(
    session_factory: sessionmaker[Session],
    make_plan: Callable[..., DailyPlan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.services.execution as execution
    import integrations.browser.manager as manager

    def forbidden(*a: Any, **k: Any) -> None:
        raise AssertionError("the daily run must not touch browser execution")

    monkeypatch.setattr(manager.BrowserManager, "__init__", forbidden)
    for name in dir(execution):
        if name.startswith(("start_", "run_", "execute")) and callable(getattr(execution, name)):
            monkeypatch.setattr(execution, name, forbidden)

    _, report = run_daily(session_factory, make_plan(), "scheduled")
    with session_factory() as db:
        assert safety(db) == (0, 0, 0)
        states = set(db.scalars(select(Job.pipeline_state)).all())
    assert states <= {S.DISCOVERED, S.SHORTLISTED, S.REJECTED_BY_AGENT, S.ANALYZED}
    assert report["safety"]["development_mode"] is True
    assert report["safety"]["before"] == report["safety"]["after"]


def test_unknown_run_id_is_an_error(
    session_factory: sessionmaker[Session], make_plan: Callable[..., DailyPlan]
) -> None:
    with pytest.raises(ValueError, match="not found"):
        DailyRunService(session_factory).execute(uuid.uuid4(), make_plan())


# --- API --------------------------------------------------------------------------------------


@pytest.fixture
def mock_only(client: TestClient, sources_config: JobSourcesConfig) -> TestClient:
    config = sources_config.model_copy(deep=True)
    for source in config.sources:
        source.enabled = source.name == "mock"
    client.app.dependency_overrides[deps.sources_config_dep] = lambda: config  # type: ignore[attr-defined]
    return client


def test_api_manual_trigger_and_status(mock_only: TestClient) -> None:
    empty = mock_only.get("/api/daily-run").json()
    assert empty["latest"] is None
    assert empty["time"] == "07:00" and empty["timezone"] == "Asia/Kolkata"
    assert empty["enabled"] is False and empty["next_run_at"] is None  # disabled in tests

    started = mock_only.post("/api/daily-run")
    assert started.status_code == 202, started.text
    assert "mock" in started.json()["message"]

    latest = mock_only.get("/api/daily-run").json()["latest"]
    assert latest["status"] == "COMPLETED" and latest["trigger"] == "manual"
    assert latest["run_id"] == started.json()["run_ids"][0]
    report = latest["report"]
    assert report["counts"]["new"] > 0 and report["counts"]["ready_to_apply"] == 0
    assert report["newly_shortlisted"]
    runs = mock_only.get("/api/agent/runs", params={"run_type": "daily"})
    assert runs.status_code == 200


def test_api_trigger_conflicts_with_an_active_run(
    mock_only: TestClient,
    session_factory: sessionmaker[Session],
    make_plan: Callable[..., DailyPlan],
) -> None:
    with session_factory() as db:
        create_daily_run(db, make_plan(), "scheduled")
    assert mock_only.post("/api/daily-run").status_code == 409


def test_api_trigger_needs_an_enabled_source(
    client: TestClient, sources_config: JobSourcesConfig
) -> None:
    config = sources_config.model_copy(deep=True)
    for source in config.sources:
        source.enabled = False
    client.app.dependency_overrides[deps.sources_config_dep] = lambda: config  # type: ignore[attr-defined]
    assert client.post("/api/daily-run").status_code == 400
