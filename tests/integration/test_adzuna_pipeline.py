"""Adzuna end to end, with mocked HTTP:
DISCOVER -> NORMALIZE -> DEDUPE -> FRESHNESS -> ROLE/LOCATION -> EXCLUSION -> ANALYSIS -> RULES.
The analyzer is the offline heuristic one (counting calls); no application is ever created."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from agent.analysis.analyzers import HeuristicAnalyzer
from backend.core.config import get_settings
from backend.core.profile import UserProfile
from backend.services.analysis import AnalysisContext, analyze_single
from backend.services.discovery import DiscoveryService, create_discovery_run
from backend.services.shortlist import load_shortlist_rules
from database.enums import Freshness, JobLogAction, JobPipelineState
from database.models import (
    AgentAction,
    Application,
    BrowserSession,
    Job,
    JobAnalysis,
    SubmissionAttempt,
)
from integrations.job_sources.adzuna import AdzunaSource
from integrations.job_sources.base import JobSource, SearchQuery
from integrations.job_sources.config import JobSourcesConfig
from integrations.job_sources.greenhouse import GreenhouseSource

NOW = datetime.now(UTC)
S = JobPipelineState
APP_ID, APP_KEY = "a1b2c3d4", "0123456789abcdef0123456789abcdef"


def adz(pid: str, title: str, company: str, place: str, hours: float | None, **over: Any) -> dict:
    area = {
        "Gurgaon": ["India", "Haryana", "Gurgaon"],
        "Noida": ["India", "Uttar Pradesh", "Noida"],
        "Singapore": ["Singapore", "Singapore", "Singapore"],  # outside India: out of scope
    }[place]
    return {
        "id": pid,
        "title": title,
        "company": {"display_name": company},
        "location": {"display_name": f"{place}, {area[1]}", "area": area},
        "description": "Build services in Python. 0-1 years of experience.",
        "redirect_url": f"https://www.adzuna.in/land/ad/{pid}?utm_source={APP_ID}",
        "created": (NOW - timedelta(hours=hours)).isoformat() if hours is not None else None,
        **over,
    }


ADZUNA_RESULTS = [
    adz("a1", "Software Engineer", "Acme Tech", "Gurgaon", 6),  # same job as Greenhouse
    adz("a2", "Software Engineer II", "Acme Tech", "Gurgaon", 6),  # similar title, other job
    adz("a3", "Graduate Engineer Trainee", "Beta Labs", "Noida", 6),  # early-career, new
    adz("a3", "Graduate Engineer Trainee", "Beta Labs", "Noida", 6),  # duplicate record
    adz("a4", "Sales Executive", "Beta Labs", "Noida", 6),  # role rejected
    adz("a5", "Software Engineer", "Gamma", "Singapore", 6),  # location rejected
    adz("a6", "Software Developer", "Acme Labs AI Pvt Ltd", "Noida", 6),  # excluded company
    adz("a7", "Data Engineer", "Delta Data", "Noida", 90),  # stale if dates are trusted
    {"title": "record without an id"},  # malformed
]


def adzuna_handler(r: httpx.Request) -> httpx.Response:
    first = r.url.params["what"] == "Software Engineer" and r.url.params["where"] == "Gurugram"
    page = r.url.path.rsplit("/", 1)[-1]
    return httpx.Response(200, json={"results": ADZUNA_RESULTS if first and page == "1" else []})


def gh_handler(r: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jobs": [
                {
                    "id": 77,
                    "title": "Software Engineer",
                    "company_name": "Acme Tech",
                    "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/77",
                    "location": {"name": "Gurugram"},
                    "first_published": (NOW - timedelta(hours=8)).isoformat(),
                    "content": "&lt;p&gt;Python&lt;/p&gt;",
                }
            ]
        },
    )


def mk(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def query(sources_config: JobSourcesConfig) -> SearchQuery:
    return sources_config.build_query(
        [],
        [],
        excluded_companies=["Acme Labs.ai", "Acme Labs AI", "Acme Labs AI Pvt Ltd"],
        max_age_hours=48,
    )


def adzuna_source(handler: Any = adzuna_handler, **kw: Any) -> AdzunaSource:
    return AdzunaSource(
        app_id=APP_ID,
        app_key=APP_KEY,
        skills_vocabulary=["Python"],
        user_agent="t",
        request_delay_seconds=0,
        client=mk(handler),
        sleep=lambda s: None,
        max_pages=1,
        results_per_page=50,
        location_queries={"Delhi NCR": "Delhi"},
        **kw,
    )


def run(factory: sessionmaker[Session], source: JobSource, query: SearchQuery) -> Any:
    with factory() as db:
        r = create_discovery_run(db, source, query, dev_mode=True)
    return DiscoveryService(factory).execute(r.id, source, query)


def completed(db: Session, run_id: Any) -> dict[str, Any]:
    action = db.scalars(
        select(AgentAction).where(
            AgentAction.run_id == run_id,
            AgentAction.tool_name == JobLogAction.DISCOVERY_COMPLETED.value,
        )
    ).one()
    return action.output or {}


@pytest.fixture
def pipeline(session_factory: sessionmaker[Session], query: SearchQuery) -> dict[str, Any]:
    gh = GreenhouseSource(
        ["acme"],
        skills_vocabulary=["Python"],
        user_agent="t",
        request_delay_seconds=0,
        fetch_pay_ranges=False,
        client=mk(gh_handler),
        sleep=lambda s: None,
    )
    return {
        "greenhouse": run(session_factory, gh, query),
        "adzuna": run(session_factory, adzuna_source(), query),
    }


def by_title(db: Session) -> dict[str, Job]:
    return {j.title: j for j in db.scalars(select(Job))}


class CountingAnalyzer(HeuristicAnalyzer):
    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.titles: list[str] = []

    def analyze(self, job: Any, profile: Any, feedback: str | None = None) -> Any:
        self.titles.append(job.title)
        return super().analyze(job, profile, feedback)


def analyze_all(db: Session, profile: UserProfile, cfg: JobSourcesConfig) -> CountingAnalyzer:
    prefs = profile.preferences.model_copy(
        update={"excluded_companies": ["Acme Labs.ai", "Acme Labs AI", "Acme Labs AI Pvt Ltd"]}
    )
    profile = profile.model_copy(update={"preferences": prefs})
    analyzer = CountingAnalyzer(cfg.search.location_patterns, cfg.search.term_aliases)
    rules = load_shortlist_rules(get_settings().shortlist_rules_path)  # the real, unchanged rules
    for job in db.scalars(select(Job)).all():
        analyze_single(
            db, job, AnalysisContext(analyzer=analyzer, profile=profile, rules=rules), dev_mode=True
        )
    db.expire_all()
    return analyzer


# --- discovery diagnostics ----------------------------------------


def test_adzuna_search_diagnostics(pipeline: dict[str, Any], db: Session) -> None:
    out = completed(db, pipeline["adzuna"].run_id)
    t = out["totals"]
    assert t["scanned"] == 9 and t["failed"] == 1  # the record without an id
    assert t["rejected_role"] == 1 and t["rejected_location"] == 1
    assert t["matched"] == 6 and t["excluded_company"] == 1
    assert t["freshness_unknown"] == 6 and t["freshness_fresh"] == 0  # 'created' not trusted
    assert t["duplicate"] == 2 and t["new"] == 4  # the Greenhouse twin + the repeated record
    # 8 roles x 4 places (Gurugram, Noida, Delhi NCR, India-wide; Remote is no Adzuna place) x 1
    assert t["pages"] == 32 and out["source"]["name"] == "adzuna"
    first = db.scalars(
        select(AgentAction)
        .where(
            AgentAction.run_id == pipeline["adzuna"].run_id,
            AgentAction.tool_name == JobLogAction.SEARCH_COMPLETED.value,
        )
        .order_by(AgentAction.sequence)
    ).first()
    assert first is not None and (first.input or {})["params"] == {
        "what": "Software Engineer",
        "where": "Gurugram",
    }
    logged = " ".join(
        str(a.input) + str(a.output) + str(a.error) for a in db.scalars(select(AgentAction))
    )
    assert APP_ID not in logged and APP_KEY not in logged


def test_failures_are_classified(
    session_factory: sessionmaker[Session], query: SearchQuery, db: Session
) -> None:
    calls: list[int] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) <= 3:  # every bounded retry of the first search times out
            raise httpx.ReadTimeout("slow", request=r)
        return httpx.Response(429)

    summary = run(session_factory, adzuna_source(handler), query)
    t = completed(db, summary.run_id)["totals"]
    assert t["failed_timeout"] == 1 and t["failed_rate_limited"] == 31  # 32 searches
    assert db.scalar(select(func.count()).select_from(Job)) == 0


# --- dedupe ----------------------------------------


def test_same_job_on_greenhouse_and_adzuna_is_one_record(
    pipeline: dict[str, Any], db: Session
) -> None:
    jobs = [j for j in db.scalars(select(Job)) if j.title == "Software Engineer"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "greenhouse"  # the original source is kept
    assert {a["source"] for a in job.source_attributions or []} == {"greenhouse", "adzuna"}


def test_similar_titles_are_different_jobs(pipeline: dict[str, Any], db: Session) -> None:
    jobs = by_title(db)
    assert "Software Engineer II" in jobs and jobs["Software Engineer II"].source == "adzuna"


def test_repeated_adzuna_record_is_stored_once(pipeline: dict[str, Any], db: Session) -> None:
    count = db.scalar(select(func.count()).select_from(Job).where(Job.source_job_id == "a3"))
    assert count == 1


# --- freshness, filtering, analysis ----------------------------------------


def test_unknown_freshness_stays_unknown_through_analysis(
    pipeline: dict[str, Any], db: Session, profile: UserProfile, sources_config: JobSourcesConfig
) -> None:
    analyze_all(db, profile, sources_config)
    trainee = by_title(db)["Graduate Engineer Trainee"]
    assert trainee.posted_at is None and trainee.posted_at_kind == "unknown"
    analysis = db.scalars(select(JobAnalysis).where(JobAnalysis.job_id == trainee.id)).one()
    assert analysis.freshness is Freshness.UNKNOWN


def test_stale_when_dates_are_trusted_never_reaches_shortlist(
    session_factory: sessionmaker[Session],
    query: SearchQuery,
    db: Session,
    profile: UserProfile,
    sources_config: JobSourcesConfig,
) -> None:
    summary = run(session_factory, adzuna_source(created_is_publication=True), query)
    t = completed(db, summary.run_id)["totals"]
    assert t["freshness_stale"] == 1 and t["freshness_fresh"] == 5
    analyze_all(db, profile, sources_config)
    stale = by_title(db)["Data Engineer"]
    assert stale.pipeline_state is S.REJECTED_BY_AGENT
    analysis = db.scalars(select(JobAnalysis).where(JobAnalysis.job_id == stale.id)).one()
    assert analysis.freshness is Freshness.STALE and not analysis.shortlisted


def test_excluded_company_never_reaches_claude(
    pipeline: dict[str, Any], db: Session, profile: UserProfile, sources_config: JobSourcesConfig
) -> None:
    analyzer = analyze_all(db, profile, sources_config)
    acme = by_title(db)["Software Developer"]
    assert acme.pipeline_state is S.REJECTED_BY_AGENT
    assert "Software Developer" not in analyzer.titles
    assert db.scalars(select(JobAnalysis).where(JobAnalysis.job_id == acme.id)).first() is None


def test_title_match_means_analyze_not_shortlist(
    pipeline: dict[str, Any], db: Session, profile: UserProfile, sources_config: JobSourcesConfig
) -> None:
    analyzer = analyze_all(db, profile, sources_config)
    assert "Graduate Engineer Trainee" in analyzer.titles  # early-career alias -> analyzed
    for job in db.scalars(select(Job)):
        assert job.pipeline_state in (S.SHORTLISTED, S.REJECTED_BY_AGENT)  # the rules decided
    assert db.scalar(select(func.count()).select_from(Application)) == 0
    assert db.scalar(select(func.count()).select_from(BrowserSession)) == 0
    assert db.scalar(select(func.count()).select_from(SubmissionAttempt)) == 0


def test_salary_is_never_fabricated(pipeline: dict[str, Any], db: Session) -> None:
    for job in db.scalars(select(Job).where(Job.source == "adzuna")):
        assert job.salary_min is None and job.salary_max is None and job.salary_raw is None
