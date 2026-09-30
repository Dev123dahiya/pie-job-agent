"""Analysis + shortlisting over mock-discovered jobs, with deterministic/fake analyzers."""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agent.analysis.analyzers import AnalyzerError, HeuristicAnalyzer, RawAnalysis
from agent.analysis.schema import AnalysisJob
from backend.core.profile import UserProfile
from backend.services.analysis import (
    AnalysisContext,
    AnalysisFailedError,
    AnalysisService,
    analyze_single,
    create_analysis_run,
    select_jobs_for_batch,
)
from backend.services.discovery import DiscoveryService, create_discovery_run
from backend.services.runs import request_cancel
from backend.services.shortlist import ShortlistRules
from database.base import utcnow
from database.enums import AgentRunStatus, JobPipelineState
from database.models import AgentAction, ApplicationEvent, Job, JobAnalysis
from integrations.job_sources.config import JobSourcesConfig
from integrations.job_sources.mock import MockSource


@pytest.fixture
def discovered(
    session_factory: sessionmaker[Session],
    mock_source: MockSource,
    sources_config: JobSourcesConfig,
) -> None:
    query = sources_config.build_query([], [])
    with session_factory() as db:
        run = create_discovery_run(db, mock_source, query, dev_mode=True)
    DiscoveryService(session_factory).execute(run.id, mock_source, query)


@pytest.fixture
def heuristic(sources_config: JobSourcesConfig) -> HeuristicAnalyzer:
    return HeuristicAnalyzer(
        sources_config.search.location_patterns, sources_config.search.term_aliases
    )


def job(db: Session, mock_id: str) -> Job:
    found = db.scalar(select(Job).where(Job.source_job_id == mock_id))
    assert found is not None
    return found


class ScriptedAnalyzer:
    """Returns queued payloads (or raises), recording the feedback it was given."""

    name = "claude_api"
    model = "fake-model"

    def __init__(self, *payloads: Any) -> None:
        self.payloads = list(payloads)
        self.feedback: list[str | None] = []
        self.calls = 0

    def analyze(
        self, job: AnalysisJob, profile: UserProfile, feedback: str | None = None
    ) -> RawAnalysis:
        self.calls += 1
        self.feedback.append(feedback)
        item = self.payloads.pop(0)
        if isinstance(item, Exception):
            raise item
        return RawAnalysis(payload=item(job) if callable(item) else item, model=self.model)


def valid(job: AnalysisJob, **overrides: Any) -> dict[str, Any]:
    return {
        "role_match": "strong",
        "skills_match": "medium",
        "experience_match": "strong",
        "location_match": "strong",
        "salary_match": "strong" if job.has_salary else "unknown",
        "freshness": job.freshness,
        "matching_skills": ["Python"],
        "missing_skills": [],
        "important_requirements": [],
        "concerns": [],
        "match_score": 80,
        "should_shortlist": True,
        "reason": "Good fit.",
        **overrides,
    }


@pytest.mark.usefixtures("discovered")
def test_batch_shortlists_by_rules(
    session_factory: sessionmaker[Session],
    db: Session,
    heuristic: HeuristicAnalyzer,
    profile: UserProfile,
) -> None:
    ids = list(
        db.scalars(
            select_jobs_for_batch(
                utcnow(), 48, include_stale=False, reanalyze=False, limit=50
            ).with_only_columns(Job.id)
        )
    )
    assert len(ids) == 6  # the 400h-old job is skipped by default
    run = create_analysis_run(db, "batch", dev_mode=True)
    ctx = AnalysisContext(analyzer=heuristic, profile=profile, rules=ShortlistRules())
    result = AnalysisService(session_factory).execute(run.id, ids, ctx)
    assert result.status is AgentRunStatus.COMPLETED
    assert (
        result.jobs_analyzed,
        result.jobs_shortlisted,
        result.jobs_rejected,
        result.jobs_failed,
    ) == (6, 3, 3, 0)

    db.expire_all()
    state = {j.source_job_id: j.pipeline_state for j in db.scalars(select(Job))}
    assert state["mock-001"] is JobPipelineState.SHORTLISTED  # strong match
    assert state["mock-003"] is JobPipelineState.SHORTLISTED  # missing salary is fine
    assert state["mock-004"] is JobPipelineState.SHORTLISTED  # missing experience is fine
    # No post date = UNKNOWN freshness = rejected (only FRESH qualifies).
    assert state["mock-012"] is JobPipelineState.REJECTED_BY_AGENT
    assert state["mock-002"] is JobPipelineState.REJECTED_BY_AGENT  # Senior Staff, 8+ years
    assert state["mock-008"] is JobPipelineState.REJECTED_BY_AGENT  # 2+ years vs fresher
    assert state["mock-007"] is JobPipelineState.DISCOVERED  # stale: not analyzed

    a = db.scalar(select(JobAnalysis).where(JobAnalysis.job_id == job(db, "mock-003").id))
    assert a is not None and a.salary_match.value == "unknown" and a.analyzer == "heuristic"
    assert a.input_snapshot and a.input_snapshot["job"]["salary_min"] is None
    assert all(r["passed"] for r in a.rule_results)


@pytest.mark.usefixtures("discovered")
def test_stale_job_rejected_by_freshness_rule(
    db: Session, heuristic: HeuristicAnalyzer, profile: UserProfile
) -> None:
    ctx = AnalysisContext(analyzer=heuristic, profile=profile, rules=ShortlistRules())
    analysis = analyze_single(db, job(db, "mock-007"), ctx, dev_mode=True)
    assert analysis is not None and analysis.freshness.value == "stale"
    assert analysis.shortlisted is False
    assert [r["rule"] for r in analysis.rule_results if not r["passed"]] == ["freshness"]
    events = [
        e.event_type
        for e in db.scalars(
            select(ApplicationEvent)
            .where(ApplicationEvent.job_id == analysis.job_id)
            .order_by(ApplicationEvent.occurred_at)
        )
    ]
    assert events == ["job_discovered", "job_analyzed", "job_rejected"]


@pytest.mark.usefixtures("discovered")
def test_malformed_output_retried_once_then_accepted(db: Session, profile: UserProfile) -> None:
    analyzer = ScriptedAnalyzer("{not json", valid)
    ctx = AnalysisContext(analyzer=analyzer, profile=profile, rules=ShortlistRules())
    analysis = analyze_single(db, job(db, "mock-001"), ctx, dev_mode=True)
    assert analysis is not None and analysis.model == "fake-model"
    assert analyzer.feedback[0] is None and "not valid JSON" in (analyzer.feedback[1] or "")


@pytest.mark.usefixtures("discovered")
def test_repeatedly_malformed_output_is_rejected_not_stored(
    db: Session, profile: UserProfile
) -> None:
    ungrounded = lambda j: valid(j, matching_skills=["Kubernetes"])  # noqa: E731 - skill not in profile
    analyzer = ScriptedAnalyzer({"role_match": "strong"}, ungrounded)
    ctx = AnalysisContext(analyzer=analyzer, profile=profile, rules=ShortlistRules())
    target = job(db, "mock-001")
    with pytest.raises(AnalysisFailedError, match="Kubernetes"):
        analyze_single(db, target, ctx, dev_mode=True)
    db.refresh(target)
    assert target.pipeline_state is JobPipelineState.DISCOVERED
    assert target.last_error and "rejected after 2 attempts" in target.last_error
    assert db.scalar(select(JobAnalysis).where(JobAnalysis.job_id == target.id)) is None
    failures = db.scalars(
        select(AgentAction).where(AgentAction.tool_name == "JOB_ANALYSIS_FAILED")
    ).all()
    assert len(failures) == 2


@pytest.mark.usefixtures("discovered")
def test_analyzer_errors_keep_the_job_retryable(db: Session, profile: UserProfile) -> None:
    ctx = AnalysisContext(
        analyzer=ScriptedAnalyzer(AnalyzerError("rate limited")),
        profile=profile,
        rules=ShortlistRules(),
    )
    target = job(db, "mock-001")
    with pytest.raises(AnalyzerError):
        analyze_single(db, target, ctx, dev_mode=True)
    assert target.pipeline_state is JobPipelineState.DISCOVERED and "rate limited" in (
        target.last_error or ""
    )


@pytest.mark.usefixtures("discovered")
def test_excluded_company_rejected_without_calling_the_analyzer(
    db: Session, profile: UserProfile
) -> None:
    profile.preferences.excluded_companies = ["Nimbus Analytics"]
    analyzer = ScriptedAnalyzer()
    ctx = AnalysisContext(analyzer=analyzer, profile=profile, rules=ShortlistRules())
    assert analyze_single(db, job(db, "mock-001"), ctx, dev_mode=True) is None
    assert analyzer.calls == 0
    assert job(db, "mock-001").pipeline_state is JobPipelineState.REJECTED_BY_AGENT


@pytest.mark.usefixtures("discovered")
def test_reanalysis_keeps_history(
    db: Session, heuristic: HeuristicAnalyzer, profile: UserProfile
) -> None:
    ctx = AnalysisContext(analyzer=heuristic, profile=profile, rules=ShortlistRules())
    target = job(db, "mock-001")
    analyze_single(db, target, ctx, dev_mode=True)
    analyze_single(db, target, ctx, dev_mode=True)
    assert len(db.scalars(select(JobAnalysis).where(JobAnalysis.job_id == target.id)).all()) == 2
    assert target.pipeline_state is JobPipelineState.SHORTLISTED


@pytest.mark.usefixtures("discovered")
def test_batch_cancellation(
    session_factory: sessionmaker[Session],
    db: Session,
    heuristic: HeuristicAnalyzer,
    profile: UserProfile,
) -> None:
    ids = list(db.scalars(select(Job.id)))
    run = create_analysis_run(db, "batch", dev_mode=True)
    request_cancel(db, run.id)
    ctx = AnalysisContext(analyzer=heuristic, profile=profile, rules=ShortlistRules())
    result = AnalysisService(session_factory).execute(run.id, ids, ctx)
    assert result.status is AgentRunStatus.STOPPED and result.jobs_analyzed == 0


@pytest.mark.usefixtures("discovered")
def test_unconfirmed_skill_claimed_as_match_is_rejected(db: Session, profile: UserProfile) -> None:
    profile.verified_profile.skills.append("Go")
    profile.verified_profile.unconfirmed_skills = ["Go"]
    claims_go = lambda j: valid(j, matching_skills=["Go"])  # noqa: E731
    ctx = AnalysisContext(
        analyzer=ScriptedAnalyzer(claims_go, claims_go), profile=profile, rules=ShortlistRules()
    )
    with pytest.raises(AnalysisFailedError, match="Go"):
        analyze_single(db, job(db, "mock-004"), ctx, dev_mode=True)


@pytest.mark.usefixtures("discovered")
def test_replace_outdated_keeps_only_current_profile_analysis(
    db: Session, heuristic: HeuristicAnalyzer, profile: UserProfile
) -> None:
    from backend.services.analysis import has_outdated_analysis, profile_fingerprint

    target = job(db, "mock-001")
    old_ctx = AnalysisContext(analyzer=heuristic, profile=profile, rules=ShortlistRules())
    analyze_single(db, target, old_ctx, dev_mode=True)

    updated = profile.model_copy(deep=True)
    updated.verified_profile.total_experience_years = 0.4
    current = profile_fingerprint(updated)
    assert has_outdated_analysis(db, target.id, current)

    new_ctx = AnalysisContext(
        analyzer=heuristic, profile=updated, rules=ShortlistRules(), replace_outdated=True
    )
    analyze_single(db, target, new_ctx, dev_mode=True)
    rows = db.scalars(select(JobAnalysis).where(JobAnalysis.job_id == target.id)).all()
    assert len(rows) == 1 and rows[0].input_snapshot["profile_sha256"] == current  # type: ignore[index]
    assert not has_outdated_analysis(db, target.id, current)
    # Job/source data untouched.
    assert target.source_job_id == "mock-001" and target.url.endswith("/1001")


@pytest.mark.usefixtures("discovered")
def test_failed_reanalysis_keeps_the_old_analysis(
    db: Session, heuristic: HeuristicAnalyzer, profile: UserProfile
) -> None:
    target = job(db, "mock-001")
    analyze_single(
        db,
        target,
        AnalysisContext(analyzer=heuristic, profile=profile, rules=ShortlistRules()),
        dev_mode=True,
    )
    updated = profile.model_copy(deep=True)
    updated.verified_profile.total_experience_years = 0.4
    failing = AnalysisContext(
        analyzer=ScriptedAnalyzer(AnalyzerError("down")),
        profile=updated,
        rules=ShortlistRules(),
        replace_outdated=True,
    )
    with pytest.raises(AnalyzerError):
        analyze_single(db, target, failing, dev_mode=True)
    assert len(db.scalars(select(JobAnalysis).where(JobAnalysis.job_id == target.id)).all()) == 1
    assert target.last_error and "down" in target.last_error


@pytest.mark.usefixtures("discovered")
def test_reanalysis_never_creates_application_records(
    db: Session, heuristic: HeuristicAnalyzer, profile: UserProfile
) -> None:
    from database.models import Application

    ctx = AnalysisContext(analyzer=heuristic, profile=profile, rules=ShortlistRules())
    analyze_single(db, job(db, "mock-001"), ctx, dev_mode=True)
    assert job(db, "mock-001").pipeline_state is JobPipelineState.SHORTLISTED
    assert db.scalars(select(Application)).all() == []
