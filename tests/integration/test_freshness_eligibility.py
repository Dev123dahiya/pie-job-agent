"""Only FRESH jobs are eligible for the shortlist (and so for READY_TO_APPLY):

    FRESH   -> eligible      STALE -> rejected      UNKNOWN -> rejected

Uses the SHIPPED config/shortlist_rules.yaml. Freshness is computed by PIE
(backend.services.freshness.compute_freshness), never by the model."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from agent.analysis.analyzers import HeuristicAnalyzer
from agent.analysis.schema import JobAnalysisOutput
from agent.tools import ToolContext, build_default_registry
from agent.tools.base import ToolErrorCode
from backend.core.config import get_settings
from backend.core.profile import UserProfile
from backend.services.analysis import AnalysisContext, analyze_single
from backend.services.discovery import DiscoveryService, create_discovery_run
from backend.services.freshness import compute_freshness
from backend.services.shortlist import ShortlistRules, evaluate, load_shortlist_rules
from database.enums import Freshness, JobPipelineState, ToolPermission
from database.models import Application, BrowserSession, Job, JobAnalysis, SubmissionAttempt
from integrations.job_sources.adzuna import AdzunaSource
from integrations.job_sources.base import SearchQuery
from integrations.job_sources.config import JobSourcesConfig

S = JobPipelineState
NOW = datetime.now(UTC)


@pytest.fixture
def rules() -> ShortlistRules:
    return load_shortlist_rules(get_settings().shortlist_rules_path)


def output(freshness: str) -> JobAnalysisOutput:
    return JobAnalysisOutput.model_validate(
        {
            "role_match": "strong",
            "skills_match": "medium",
            "experience_match": "medium",
            "location_match": "strong",
            "salary_match": "unknown",
            "freshness": freshness,
            "matching_skills": [],
            "missing_skills": [],
            "important_requirements": [],
            "concerns": [],
            "match_score": 70,
            "should_shortlist": True,
            "reason": "ok",
        }
    )


def failed(results: list[dict[str, Any]]) -> list[str]:
    return [r["rule"] for r in results if not r["passed"]]


# 1-3: the three freshness levels under the shipped rules --------------------------------


def test_fresh_is_eligible(rules: ShortlistRules) -> None:
    ok, results = evaluate(output("fresh"), rules)
    assert ok and "freshness" not in failed(results)


@pytest.mark.parametrize("level", ["stale", "unknown"])
def test_stale_and_unknown_are_rejected(rules: ShortlistRules, level: str) -> None:
    ok, results = evaluate(output(level), rules)
    assert not ok and failed(results) == ["freshness"]  # rejected for freshness alone


# 6: the 48-hour boundary is unchanged ---------------------------------------------------


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(hours=1), Freshness.FRESH),
        (timedelta(hours=47, minutes=59), Freshness.FRESH),
        (timedelta(hours=48), Freshness.FRESH),  # exactly 48h: still inside the window
        (timedelta(hours=48, seconds=1), Freshness.STALE),
        (timedelta(days=30), Freshness.STALE),
    ],
)
def test_48_hour_boundary_is_unchanged(age: timedelta, expected: Freshness) -> None:
    assert compute_freshness(NOW - age, NOW, 48) is expected
    assert compute_freshness(None, NOW, 48) is Freshness.UNKNOWN


def test_window_is_still_48_hours() -> None:
    from backend.core.profile import load_user_profile

    assert load_user_profile().preferences.maximum_application_age_hours == 48


# 7: every other rule is unchanged --------------------------------------------------------


def test_other_rules_are_unchanged(rules: ShortlistRules) -> None:
    dims = {name: rule.reject for name, rule in rules.dimensions.items()}
    assert dims == {
        "role_match": ["weak"],
        "experience_match": ["weak"],
        "location_match": ["weak"],
        "skills_match": [],
        "salary_match": ["weak"],  # unknown salary is still not a rejection
    }
    assert rules.min_match_score is None and rules.max_missing_skills is None
    assert rules.require_analyzer_recommendation is True
    assert rules.analyze_stale_jobs_in_batch is False
    ok, _ = evaluate(output("fresh"), rules)  # unknown salary + fresh: still eligible
    assert ok
    for dimension in ("role_match", "experience_match", "location_match", "salary_match"):
        bad = output("fresh").model_copy(update={dimension: "weak"})
        assert failed(evaluate(bad, rules)[1]) == [dimension]


# 4-5: jobs without a trustworthy post date end up rejected, never READY_TO_APPLY ---------


def adzuna_handler(r: httpx.Request) -> httpx.Response:
    first = r.url.params["what"] == "Software Engineer" and r.url.params["where"] == "Noida"
    records = [
        {
            "id": "z1",
            "title": "Graduate Software Engineer",
            "company": {"display_name": "Acme Tech"},
            "location": {"display_name": "Noida", "area": ["India", "Uttar Pradesh", "Noida"]},
            "description": "Python. 0-1 years of experience.",
            # Present, but undocumented - so not trusted as a post date.
            "created": (NOW - timedelta(hours=2)).isoformat(),
        }
    ]
    return httpx.Response(200, json={"results": records if first else []})


@pytest.fixture
def adzuna_job(session_factory: sessionmaker[Session], sources_config: JobSourcesConfig) -> Any:
    query: SearchQuery = sources_config.build_query([], [])
    source = AdzunaSource(
        app_id="a1b2c3d4",
        app_key="0123456789abcdef",
        skills_vocabulary=["Python"],
        user_agent="t",
        request_delay_seconds=0,
        max_pages=1,
        client=httpx.Client(transport=httpx.MockTransport(adzuna_handler)),
        sleep=lambda s: None,
    )
    with session_factory() as db:
        run = create_discovery_run(db, source, query, dev_mode=True)
    DiscoveryService(session_factory).execute(run.id, source, query)
    with session_factory() as db:
        job = db.scalars(select(Job).where(Job.source == "adzuna")).one()
        return job.id


def analyze(db: Session, job: Job, profile: UserProfile, cfg: JobSourcesConfig) -> JobAnalysis:
    analyzer = HeuristicAnalyzer(cfg.search.location_patterns, cfg.search.term_aliases)
    rules = load_shortlist_rules(get_settings().shortlist_rules_path)
    analysis = analyze_single(
        db, job, AnalysisContext(analyzer=analyzer, profile=profile, rules=rules), dev_mode=True
    )
    assert analysis is not None
    return analysis


def test_no_trustworthy_timestamp_stays_unknown_and_is_rejected(
    db: Session, adzuna_job: Any, profile: UserProfile, sources_config: JobSourcesConfig
) -> None:
    job = db.get(Job, adzuna_job)
    assert job is not None
    assert job.posted_at is None and job.posted_at_kind == "unknown"  # 'created' not trusted
    analysis = analyze(db, job, profile, sources_config)
    assert analysis.freshness is Freshness.UNKNOWN
    assert not analysis.shortlisted
    assert "freshness" in failed(analysis.rule_results)
    assert job.pipeline_state is S.REJECTED_BY_AGENT


def test_adzuna_unknown_freshness_can_never_become_ready_to_apply(
    db: Session,
    adzuna_job: Any,
    profile: UserProfile,
    sources_config: JobSourcesConfig,
    tailor_settings: Any,
) -> None:
    job = db.get(Job, adzuna_job)
    assert job is not None
    analyze(db, job, profile, sources_config)
    registry = build_default_registry()
    ctx = ToolContext(
        db=db,
        settings=tailor_settings,
        profile=profile,
        allowed=frozenset({ToolPermission.READ_ONLY, ToolPermission.WRITE}),
    )
    draft = registry.execute("create_application_draft", {"job_id": str(job.id)}, ctx)
    assert draft.error_code is ToolErrorCode.PRECONDITION_FAILED  # not SHORTLISTED
    ready = registry.execute(
        "update_application_status",
        {"job_id": str(job.id), "target_status": "READY_TO_APPLY", "reason": "try"},
        ctx,
    )
    assert not ready.ok
    db.expire_all()
    assert db.get(Job, adzuna_job).pipeline_state is S.REJECTED_BY_AGENT  # type: ignore[union-attr]
    assert db.scalar(select(func.count()).select_from(Application)) == 0
    assert db.scalar(select(func.count()).select_from(BrowserSession)) == 0
    assert db.scalar(select(func.count()).select_from(SubmissionAttempt)) == 0


# 9: removing or ignoring the config file cannot make UNKNOWN (or STALE) eligible again -------


@pytest.mark.parametrize(
    "content",
    [
        None,  # the file does not exist
        "",  # empty file
        "min_match_score: null\n",  # no freshness section at all
        "freshness: {}\n",  # freshness section without a reject list
    ],
)
def test_missing_or_partial_config_still_rejects_unknown(
    tmp_path: Any, content: str | None
) -> None:
    path = tmp_path / "shortlist_rules.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    fallback = load_shortlist_rules(path)
    assert fallback.freshness.reject == ["stale", "unknown"]
    assert evaluate(output("fresh"), fallback)[0]
    for level in ("stale", "unknown"):
        ok, results = evaluate(output(level), fallback)
        assert not ok and failed(results) == ["freshness"], level


def test_shipped_file_and_code_default_agree(rules: ShortlistRules) -> None:
    assert rules.freshness.reject == ShortlistRules().freshness.reject == ["stale", "unknown"]
