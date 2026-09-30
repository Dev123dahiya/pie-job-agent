"""New sources end to end: DISCOVER -> NORMALIZE -> DEDUPLICATE -> ANALYZE -> SHORTLIST.

HTTP is mocked (no network); the analyzer is the offline heuristic one, wrapped to count
calls. Nothing is applied to and no application drafts are created."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from agent.analysis.analyzers import HeuristicAnalyzer
from backend.core.profile import UserProfile
from backend.services.analysis import AnalysisContext, analyze_single
from backend.services.discovery import DiscoveryService, create_discovery_run
from backend.services.shortlist import ShortlistRules, load_shortlist_rules
from database.enums import AgentRunStatus, Freshness, JobLogAction, JobPipelineState
from database.models import AgentAction, Application, Job, JobAnalysis
from integrations.job_sources.ashby import AshbySource
from integrations.job_sources.base import JobSource, SearchQuery
from integrations.job_sources.config import JobSourcesConfig
from integrations.job_sources.lever import LeverSource

NOW = datetime.now(UTC)
S = JobPipelineState


def ms(hours_ago: float) -> int:
    return int((NOW - timedelta(hours=hours_ago)).timestamp() * 1000)


def lever_posting(
    pid: str, title: str, location: str, created: int | None, **over: Any
) -> dict[str, Any]:
    return {
        "id": pid,
        "text": title,
        "categories": {"location": location},
        "country": "IN",
        "workplaceType": "onsite",
        "createdAt": created,
        "hostedUrl": f"https://jobs.lever.co/x/{pid}",
        "applyUrl": f"https://jobs.lever.co/x/{pid}/apply",
        "description": "<p>Build backend services in Python and Java. 0-1 years of experience.</p>",
        **over,
    }


LEVER_BOARDS: dict[str, list[dict[str, Any]]] = {
    "acme": [
        lever_posting("fresh", "Software Engineer I", "Noida", ms(5)),
        lever_posting("stale", "Software Engineer", "Gurugram", ms(72)),
        lever_posting("undated", "Backend Engineer", "Noida", None),
        lever_posting("sales", "Sales Manager", "Noida", ms(5)),
        lever_posting("sg", "Software Engineer", "Singapore", ms(5), country="SG"),  # not India
    ],
    "acmelabs": [lever_posting("eth", "Software Engineer", "Gurugram", ms(5))],
    "acmelabspvt": [lever_posting("eth2", "Software Developer", "Noida", ms(5))],
    "ethsci": [lever_posting("es", "Data Engineer", "Noida", ms(5))],
}


def lever_handler(r: httpx.Request) -> httpx.Response:
    slug = r.url.path.rstrip("/").rsplit("/", 1)[-1]
    return httpx.Response(
        200, json=LEVER_BOARDS.get(slug, []) if r.url.params.get("skip") == "0" else []
    )


def ashby_handler(r: httpx.Request) -> httpx.Response:
    # The same "fresh" role, listed again on Ashby by the same company.
    return httpx.Response(
        200,
        json={
            "jobs": [
                {
                    "id": "dup",
                    "title": "Software Engineer I",
                    "location": "Noida",
                    "address": {"postalAddress": {"addressCountry": "India"}},
                    "isListed": True,
                    "publishedAt": (NOW - timedelta(hours=4)).isoformat(),
                    "descriptionHtml": "<p>Python</p>",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/dup",
                }
            ]
        },
    )


def mk(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def query(sources_config: JobSourcesConfig) -> SearchQuery:
    return sources_config.build_query([], [])


@pytest.fixture
def excluding_profile(profile: UserProfile) -> UserProfile:
    prefs = profile.preferences.model_copy(
        update={
            "excluded_companies": ["Acme Labs.ai", "Acme Labs AI", "Acme Labs AI Pvt Ltd"],
            "preferred_locations": ["Gurugram", "Noida", "Delhi NCR", "Remote"],
            "maximum_application_age_hours": 48,
        }
    )
    return profile.model_copy(update={"preferences": prefs})


class CountingAnalyzer(HeuristicAnalyzer):
    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.analyzed: list[str] = []

    def analyze(self, job: Any, profile: Any, feedback: str | None = None) -> Any:
        self.analyzed.append(job.title)
        return super().analyze(job, profile, feedback)


def discover(factory: sessionmaker[Session], source: JobSource, query: SearchQuery) -> Any:
    with factory() as db:
        run = create_discovery_run(db, source, query, dev_mode=True)
    return DiscoveryService(factory).execute(run.id, source, query)


@pytest.fixture
def discovered(session_factory: sessionmaker[Session], query: SearchQuery) -> dict[str, Any]:
    lever = LeverSource(
        [
            {"slug": "acme", "name": "Acme"},
            {"slug": "acmelabs", "name": "Acme Labs.ai"},
            {"slug": "acmelabspvt", "name": "Acme Labs AI Pvt Ltd"},
            {"slug": "ethsci", "name": "Acme Labs Sciences"},
        ],
        skills_vocabulary=["Python", "Java"],
        user_agent="t",
        request_delay_seconds=0,
        client=mk(lever_handler),
        sleep=lambda s: None,
    )
    ashby = AshbySource(
        [{"slug": "acme", "name": "Acme"}],
        skills_vocabulary=["Python"],
        user_agent="t",
        request_delay_seconds=0,
        client=mk(ashby_handler),
        sleep=lambda s: None,
    )
    return {
        "lever": discover(session_factory, lever, query),
        "ashby": discover(session_factory, ashby, query),
    }


def by_id(db: Session) -> dict[str, Job]:
    return {j.source_job_id.split(":")[-1]: j for j in db.scalars(select(Job))}


def analyze_all(
    db: Session, profile: UserProfile, sources_config: JobSourcesConfig
) -> CountingAnalyzer:
    analyzer = CountingAnalyzer(
        sources_config.search.location_patterns, sources_config.search.term_aliases
    )
    from backend.core.config import get_settings

    rules = load_shortlist_rules(get_settings().shortlist_rules_path)  # the real rules
    for job in db.scalars(select(Job)).all():
        analyze_single(
            db, job, AnalysisContext(analyzer=analyzer, profile=profile, rules=rules), dev_mode=True
        )
    db.expire_all()
    return analyzer


def test_discovery_counts_role_and_location_rejections(
    discovered: dict[str, Any], db: Session
) -> None:
    lever = discovered["lever"]
    assert lever.status is AgentRunStatus.COMPLETED
    done = db.scalars(
        select(AgentAction).where(
            AgentAction.run_id == lever.run_id,
            AgentAction.tool_name == JobLogAction.DISCOVERY_COMPLETED.value,
        )
    ).one()
    totals = (done.output or {})["totals"]
    assert totals["rejected_role"] == 1 and totals["rejected_location"] == 1  # Sales; Singapore
    assert totals["new"] == 6 and (done.output or {})["source"]["name"] == "lever"
    assert set(by_id(db)) == {"fresh", "stale", "undated", "eth", "eth2", "es"}


def test_same_job_on_two_sources_is_one_record_with_both_attributions(
    discovered: dict[str, Any], db: Session
) -> None:
    assert discovered["ashby"].jobs_duplicate == 1 and discovered["ashby"].jobs_new == 0
    job = by_id(db)["fresh"]
    assert job.source == "lever"  # the stored record is unchanged
    sources = {(a["source"], a["source_job_id"]) for a in job.source_attributions or []}
    assert sources == {("lever", "acme:fresh"), ("ashby", "acme:dup")}
    assert (
        db.scalar(select(func.count()).select_from(Job).where(Job.title == "Software Engineer I"))
        == 1
    )


def test_stale_jobs_never_reach_the_shortlist(
    discovered: dict[str, Any],
    db: Session,
    excluding_profile: UserProfile,
    sources_config: JobSourcesConfig,
) -> None:
    analyze_all(db, excluding_profile, sources_config)
    stale = by_id(db)["stale"]
    assert stale.pipeline_state is S.REJECTED_BY_AGENT
    analysis = db.scalars(select(JobAnalysis).where(JobAnalysis.job_id == stale.id)).one()
    assert analysis.freshness is Freshness.STALE and not analysis.shortlisted


def test_excluded_acme_labs_variants_never_reach_analysis_or_shortlist(
    discovered: dict[str, Any],
    db: Session,
    excluding_profile: UserProfile,
    sources_config: JobSourcesConfig,
) -> None:
    analyzer = analyze_all(db, excluding_profile, sources_config)
    jobs = by_id(db)
    for key in ("eth", "eth2"):
        assert jobs[key].pipeline_state is S.REJECTED_BY_AGENT
    analyzed_ids = set(db.scalars(select(JobAnalysis.job_id)).all())
    assert jobs["eth"].id not in analyzed_ids and jobs["eth2"].id not in analyzed_ids
    assert len(analyzer.analyzed) == 4  # fresh, stale, undated and Acme Labs Sciences only
    assert jobs["es"].id in analyzed_ids  # a similarly named, unrelated company is not excluded


def test_fresh_entry_level_jobs_reach_analysis(
    discovered: dict[str, Any],
    db: Session,
    excluding_profile: UserProfile,
    sources_config: JobSourcesConfig,
) -> None:
    analyzer = analyze_all(db, excluding_profile, sources_config)
    fresh = by_id(db)["fresh"]
    assert "Software Engineer I" in analyzer.analyzed
    analysis = db.scalars(select(JobAnalysis).where(JobAnalysis.job_id == fresh.id)).one()
    assert analysis.freshness is Freshness.FRESH
    assert fresh.pipeline_state in (S.SHORTLISTED, S.REJECTED_BY_AGENT)  # decided by the rules
    assert db.scalar(select(func.count()).select_from(Application)) == 0  # nothing applied to


def test_unknown_freshness_stays_unknown(
    discovered: dict[str, Any],
    db: Session,
    excluding_profile: UserProfile,
    sources_config: JobSourcesConfig,
) -> None:
    analyze_all(db, excluding_profile, sources_config)
    undated = by_id(db)["undated"]
    assert undated.posted_at is None and undated.posted_at_kind == "unknown"
    analysis = db.scalars(select(JobAnalysis).where(JobAnalysis.job_id == undated.id)).one()
    assert analysis.freshness is Freshness.UNKNOWN


def test_salary_is_never_fabricated(discovered: dict[str, Any], db: Session) -> None:
    for job in db.scalars(select(Job)):
        assert job.salary_min is None and job.salary_max is None and job.salary_currency is None
        assert job.salary_raw is None  # nothing disclosed, nothing stored


def test_timestamp_kind_is_recorded(discovered: dict[str, Any], db: Session) -> None:
    jobs = by_id(db)
    assert jobs["fresh"].posted_at_kind == "created" and jobs["undated"].posted_at_kind == "unknown"


def test_default_rules_are_unchanged() -> None:
    from backend.core.config import get_settings

    rules = load_shortlist_rules(get_settings().shortlist_rules_path)
    assert rules.freshness.reject == ["stale", "unknown"]
    assert "weak" in rules.dimensions["experience_match"].reject  # type: ignore[index]
    assert "weak" in rules.dimensions["salary_match"].reject  # type: ignore[index]
    assert isinstance(rules, ShortlistRules)


def test_undisclosed_salary_is_stored_as_sql_null(discovered: dict[str, Any], db: Session) -> None:
    assert db.scalar(select(func.count()).select_from(Job).where(Job.salary_raw.is_(None))) == 6
