# ruff: noqa: F811 - test functions take the imported shared fixtures as parameters.
"""Candidate fit in the resolution queue: employers whose own leads show entry-level
evidence are looked up first, senior-only employers last. Order only - eligibility,
freshness, verification and matching are untouched. HTTP is mocked; requests recorded."""

from datetime import timedelta

import httpx2 as httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.core.config import get_settings
from backend.core.profile import load_user_profile
from backend.services.daily import build_daily_plan
from backend.services.freshness import compute_freshness
from backend.services.leads import (
    FIT_ENTRY,
    FIT_SENIOR_ONLY,
    FIT_UNKNOWN,
    TIER_FREE,
    adzuna_lead_records,
    configured_boards,
    known_misses,
    register_leads,
    resolution_queue,
    trusted_board_index,
)
from database.base import utcnow
from database.enums import Freshness, LeadStatus
from database.models import Job, JobLead
from integrations.job_sources.base import SearchQuery
from integrations.job_sources.config import JobSourcesConfig, LeadResolutionConfig
from tests.integration.test_leads import (  # noqa: F401
    NOW,
    World,
    _no_application_side_effects,
    add_adzuna_row,
    client,
    leads_plan,
    mocked_crawler,
    query,
    step,
    world,
)

YEARS = 0.4
ENTRY = "Build APIs in Python. Freshers welcome to apply."
SENIOR = "Build APIs in Python. 5+ years of experience in backend development required."
NEUTRAL = "Build APIs in Python and PostgreSQL for our India team."


def add(factory: sessionmaker[Session], company: str, title: str, text: str, n: int = 1) -> None:
    with factory() as db:
        for i in range(n):
            sid = f"{company}-{title}-{i}"
            add_adzuna_row(db, company=company, sid=sid, title=title, description=text)


def queue(
    factory: sessionmaker[Session], cfg: JobSourcesConfig, years: float | None = YEARS
) -> list[tuple[str, int, int, int]]:
    with factory() as db:
        metrics = dict.fromkeys(("leads_discovered", "leads_filtered", "leads_already_known",
                                 "leads_registered", "leads_rejected"), 0)  # fmt: skip
        register_leads(db, adzuna_lead_records(db, NOW - timedelta(days=1)),
                       cfg.build_query([], []), metrics)  # fmt: skip
        db.commit()
        leads = db.scalars(select(JobLead).where(JobLead.status == LeadStatus.NEW)).all()
        items = resolution_queue(
            db, list(leads), LeadResolutionConfig(enabled=True), configured_boards(cfg),
            trusted_board_index(db), known_misses(db), years,
        )  # fmt: skip
        return [(i.normalized, i.tier, i.fit, i.cost) for i in items]


def test_entry_evidence_first_senior_only_last_whatever_the_cost(
    session_factory: sessionmaker[Session], sources_config: JobSourcesConfig
) -> None:
    add(session_factory, "Cheap", "Senior Software Engineer", SENIOR)  # 1 guess, senior-only
    add(session_factory, "Neutral Works", "Software Engineer", NEUTRAL)  # 2 guesses
    add(session_factory, "Fresh Hire Labs", "Software Engineer", ENTRY)  # entry evidence
    add(session_factory, "Juniors Hub", "Graduate Software Engineer", NEUTRAL)  # entry title
    assert queue(session_factory, sources_config) == [
        ("fresh hire labs", 3, FIT_ENTRY, 8),
        ("juniors hub", 3, FIT_ENTRY, 8),
        ("neutral works", 3, FIT_UNKNOWN, 8),
        ("cheap", 3, FIT_SENIOR_ONLY, 4),  # cheapest, but its postings cannot fit
    ]


def test_one_entry_lead_makes_the_company_entry_fit(
    session_factory: sessionmaker[Session], sources_config: JobSourcesConfig
) -> None:
    add(session_factory, "Mixed Co Systems", "Senior Data Engineer", SENIOR, n=5)
    add(session_factory, "Mixed Co Systems", "Data Engineer I", NEUTRAL)
    add(session_factory, "Senior Shop Tech", "Staff Software Engineer", SENIOR, n=5)
    order = queue(session_factory, sources_config)
    assert [(c, f) for c, _, f, _ in order] == [
        ("mixed co systems", FIT_ENTRY),
        ("senior shop tech", FIT_SENIOR_ONLY),
    ]


def test_fit_never_overrides_the_free_tier(
    session_factory: sessionmaker[Session], sources_config: JobSourcesConfig
) -> None:
    add(session_factory, "MongoDB", "Senior Software Engineer", SENIOR)  # configured, senior
    add(session_factory, "Fresh Hire Labs", "Software Engineer", ENTRY)
    first = queue(session_factory, sources_config)[0]
    assert first[:3] == ("mongodb", TIER_FREE, FIT_SENIOR_ONLY) and first[3] == 0


def test_verified_years_come_from_the_profile(sources_config: JobSourcesConfig) -> None:
    config = sources_config.model_copy(deep=True)
    config.lead_resolution.enabled = True
    plan = build_daily_plan(get_settings(), config=config)
    assert plan.leads is not None
    assert plan.leads.candidate_years == load_user_profile().verified_profile.total_experience_years


def test_without_profile_years_explicit_requirements_do_not_rank(
    session_factory: sessionmaker[Session], sources_config: JobSourcesConfig
) -> None:
    add(session_factory, "Years Ask Systems", "Software Engineer", SENIOR)
    assert queue(session_factory, sources_config, years=None)[0][2] == FIT_UNKNOWN


def test_fit_ordering_spends_the_budget_on_fitting_employers_first(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    add(session_factory, "Senior Shop Tech", "Staff Software Engineer", SENIOR, n=10)
    add(session_factory, "Acme Tech", "Software Engineer", ENTRY)  # has a verifiable board
    plan = leads_plan(sources_config, client, max_requests_per_run=4)
    plan.candidate_years = YEARS
    outcome = step(session_factory, plan, query)
    assert world.requests[0] == "https://boards-api.greenhouse.io/v1/boards/acmetech"
    assert outcome.metrics["boards_verified"] == 1
    assert outcome.metrics["companies_entry_evidence"] == 1
    assert outcome.metrics["companies_senior_only"] == 1
    with session_factory() as db:
        senior = db.scalars(select(JobLead).where(JobLead.company_name == "Senior Shop Tech")).all()
        assert {lead.status for lead in senior} == {LeadStatus.NEW}  # deferred, not dropped


def test_fit_is_order_only_nothing_about_jobs_or_leads_changes(
    session_factory: sessionmaker[Session], sources_config: JobSourcesConfig
) -> None:
    add(session_factory, "Fresh Hire Labs", "Software Engineer", ENTRY)
    add(session_factory, "Senior Shop Tech", "Staff Software Engineer", SENIOR)
    queue(session_factory, sources_config)
    with session_factory() as db:
        for job in db.scalars(select(Job)):
            assert job.posted_at is None
            assert compute_freshness(job.posted_at, utcnow(), 48) is Freshness.UNKNOWN
        for lead in db.scalars(select(JobLead)):
            assert lead.status is LeadStatus.NEW and not lead.aggregator_timestamp_trusted


def test_lead_summary_api_reports_state_read_only(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
) -> None:
    from fastapi.testclient import TestClient

    from backend.api import deps
    from backend.main import create_app
    from database.enums import BoardStatus
    from database.models import EmployerBoard
    from database.session import get_db

    with session_factory() as db:
        db.add_all(
            [
                EmployerBoard(normalized_company="acme tech", company_name="Acme Tech",
                              platform="greenhouse", board_id="acmetech",
                              status=BoardStatus.VERIFIED, active=False),
                EmployerBoard(normalized_company="globex", company_name="Globex",
                              platform="greenhouse", board_id="globex",
                              status=BoardStatus.CANDIDATE),
                EmployerBoard(normalized_company="nowhere", company_name="Nowhere",
                              status=BoardStatus.NOT_FOUND,
                              next_check_at=utcnow() + timedelta(days=3)),
            ]
        )  # fmt: skip
        db.commit()
    add(session_factory, "Fresh Hire Labs", "Software Engineer", ENTRY)
    queue(session_factory, sources_config)  # registers one lead

    def _db():  # type: ignore[no-untyped-def]
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    app = create_app()
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[deps.sources_config_dep] = lambda: sources_config
    with TestClient(app) as api:
        body = api.get("/api/leads/summary").json()
    assert body["enabled"] is False and body["auto_activate_verified"] is False
    assert body["max_requests_per_run"] == 60 and body["max_requests_per_company"] == 8
    assert body["leads_total"] == 1 and body["leads_by_status"]["NEW"] == 1
    assert body["boards_by_status"] == {
        "CANDIDATE": 1,
        "VERIFIED": 1,
        "REJECTED": 0,
        "NOT_FOUND": 1,
    }
    assert body["negative_cache_active"] == 1
    assert sorted(b["board_id"] for b in body["boards_needing_review"]) == ["acmetech", "globex"]
    assert body["active_boards"] == []
    with session_factory() as db:  # read-only: nothing changed
        assert db.scalar(select(EmployerBoard).where(EmployerBoard.active)) is None
