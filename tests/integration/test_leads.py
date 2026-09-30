"""Phase 5.7: aggregator lead -> trusted ATS board -> strict match -> the existing pipeline.

All HTTP is mocked (every requested URL is recorded, so tests can prove what was NOT
requested). The analyzer is the offline heuristic one; the shortlist rules are the shipped
ones. No application, browser session or submission is ever created."""

import logging
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from agent.analysis.analyzers import HeuristicAnalyzer
from backend.core.config import get_settings
from backend.core.profile import UserProfile
from backend.scripts import leads as leads_cli
from backend.services import leads as leads_service
from backend.services.daily import DailyPlan, build_daily_plan, run_daily
from backend.services.discovery import DiscoveryService, create_discovery_run
from backend.services.freshness import compute_freshness
from backend.services.leads import LeadsPlan, run_leads_step
from backend.services.runs import RunLogger
from backend.services.shortlist import load_shortlist_rules
from database.base import utcnow
from database.enums import (
    AgentRunStatus,
    BoardStatus,
    Freshness,
    JobPipelineState,
    LeadStatus,
    RunType,
)
from database.models import (
    AgentAction,
    AgentRun,
    Application,
    BrowserSession,
    EmployerBoard,
    Job,
    JobAnalysis,
    JobLead,
    SubmissionAttempt,
)
from integrations.job_sources.adzuna import AdzunaSource
from integrations.job_sources.base import SearchQuery
from integrations.job_sources.config import JobSourcesConfig, LeadResolutionConfig
from integrations.job_sources.greenhouse import GreenhouseSource

NOW = utcnow()
APP_ID, APP_KEY = "a1b2c3d4", "0123456789abcdef0123456789abcdef"
EXCLUDED = ["Acme Labs.ai", "Acme Labs AI", "Acme Labs AI Pvt Ltd"]
DESC = (
    "Acme Tech is hiring a Software Engineer to build backend services in Python and FastAPI. "
    "You will design REST APIs, write tests, work with PostgreSQL and ship features to "
    "millions of users across India. We value ownership, clear writing and pragmatic "
    "engineering. Freshers with strong fundamentals in data structures are welcome to apply."
)


def iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


# --- a small fake internet: the supported ATS APIs + Himalayas + Adzuna ---------------------


class World:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.gh_boards: dict[str, dict[str, Any]] = {
            "acmetech": {
                "name": "Acme Tech",
                "jobs": [
                    {
                        "id": 101,
                        "title": "Software Engineer",
                        "company_name": "Acme Tech",
                        "absolute_url": "https://job-boards.greenhouse.io/acmetech/jobs/101",
                        "location": {"name": "Bengaluru, India"},
                        "first_published": iso(30),
                        "content": f"&lt;p&gt;{DESC}&lt;/p&gt;",
                    },
                    {
                        "id": 102,
                        "title": "Data Engineer",
                        "company_name": "Acme Tech",
                        "absolute_url": "https://job-boards.greenhouse.io/acmetech/jobs/102",
                        "location": {"name": "Pune, India"},
                        "first_published": iso(24 * 120),  # four months old
                        "content": "&lt;p&gt;Spark pipelines.&lt;/p&gt;",
                    },
                ],
            },
            "globex": {"name": "Globex Industries International", "jobs": []},
        }
        self.lever: dict[str, list[dict[str, Any]]] = {
            "umbrella": [{"descriptionPlain": "We build things."}] * 4,
            "ab": [{"descriptionPlain": "Ab is great."}] * 5,
        }
        self.ashby: dict[str, list[dict[str, Any]]] = {
            "initech": [{"descriptionPlain": "Initech builds TPS software. Join Initech."}] * 4,
        }
        self.sr_status = 404
        self.himalayas_items: list[dict[str, Any]] = []

    def __call__(self, r: httpx.Request) -> httpx.Response:
        url = str(r.url)
        self.requests.append(url)
        host, path = r.url.host, r.url.path.rstrip("/")
        if host == "boards-api.greenhouse.io":
            parts = path.split("/")  # /v1/boards/<board>[/jobs]
            board = self.gh_boards.get(parts[3]) if len(parts) > 3 else None
            if board is None:
                return httpx.Response(404)
            if path.endswith("/jobs"):
                return httpx.Response(200, json={"jobs": board["jobs"]})
            return httpx.Response(200, json={"name": board["name"]})
        if host == "api.lever.co":
            data = self.lever.get(path.split("/")[-1])
            return httpx.Response(200, json=data) if data else httpx.Response(404)
        if host == "api.ashbyhq.com":
            data = self.ashby.get(path.split("/")[-1])
            return httpx.Response(200, json={"jobs": data}) if data else httpx.Response(404)
        if host == "api.smartrecruiters.com":
            return httpx.Response(self.sr_status, json={"content": []})
        if host == "himalayas.app" and path == "/jobs/api/search":
            return httpx.Response(200, json={"jobs": self.himalayas_items})
        if host == "api.adzuna.com":
            first = r.url.params.get("what") == "Software Engineer" and r.url.path.endswith("/1")
            return httpx.Response(200, json={"results": ADZUNA if first else []})
        return httpx.Response(599)  # anything else is a test failure (asserted below)


ADZUNA = [
    {
        "id": "z1",
        "title": "Software Engineer",
        "company": {"display_name": "Acme Tech Pvt Ltd"},
        "location": {
            "display_name": "Bangalore, Karnataka",
            "area": ["India", "Karnataka", "Bangalore"],
        },
        "description": DESC,
        "redirect_url": f"https://www.adzuna.in/land/ad/z1?utm_source={APP_ID}&se={APP_KEY}",
        "created": iso(1),  # looks fresh - and must never count
    }
]


@pytest.fixture
def world() -> World:
    return World()


class SharedClient(httpx.Client):
    """One mock client shared by every adapter/probe in a test (they close their clients)."""

    def close(self) -> None:
        pass


@pytest.fixture
def client(world: World) -> httpx.Client:  # shadows the API client fixture on purpose
    return SharedClient(transport=httpx.MockTransport(world))


@pytest.fixture
def query(sources_config: JobSourcesConfig) -> SearchQuery:
    return sources_config.build_query([], [], excluded_companies=EXCLUDED, max_age_hours=48)


def rules(**over: Any) -> LeadResolutionConfig:
    return LeadResolutionConfig(enabled=True, request_delay_seconds=0, **over)


def leads_plan(sources_config: JobSourcesConfig, client: httpx.Client, **over: Any) -> LeadsPlan:
    return LeadsPlan(
        rules=rules(**over), sources_config=sources_config, settings=get_settings(),
        client=client, sleep=lambda s: None,
    )  # fmt: skip


@pytest.fixture(autouse=True)
def mocked_crawler(monkeypatch: pytest.MonkeyPatch, client: httpx.Client) -> None:
    """Board crawls use the real Greenhouse adapter, on the fake network."""

    def build(platform: str, boards: list[tuple[str, str]], config: Any, settings: Any) -> Any:
        assert platform == "greenhouse"
        return GreenhouseSource(
            [b for b, _ in boards], skills_vocabulary=["Python"], user_agent="t",
            request_delay_seconds=0, fetch_pay_ranges=False, client=client, sleep=lambda s: None,
        )  # fmt: skip

    monkeypatch.setattr(leads_service, "build_source_for_boards", build)


def gh_source(client: httpx.Client) -> GreenhouseSource:
    return GreenhouseSource(
        ["acmetech"],
        skills_vocabulary=[],
        user_agent="t",
        request_delay_seconds=0,
        fetch_pay_ranges=False,
        client=client,
        sleep=lambda s: None,
    )


def adzuna(client: httpx.Client) -> AdzunaSource:
    return AdzunaSource(
        app_id=APP_ID, app_key=APP_KEY, skills_vocabulary=["Python"], user_agent="t",
        request_delay_seconds=0, client=client, sleep=lambda s: None, max_pages=1,
    )  # fmt: skip


def add_adzuna_row(db: Session, company: str = "Acme Tech Pvt Ltd", **over: Any) -> Job:
    from database.repositories import CompanyRepository

    job = Job(
        source="adzuna",
        source_job_id=over.pop("sid", f"z-{uuid.uuid4().hex[:6]}"),
        company=CompanyRepository(db).get_or_create(company),
        title=over.pop("title", "Software Engineer"),
        location=over.pop("location", "Bangalore, Karnataka, India"),
        description=over.pop("description", DESC),
        url="https://www.adzuna.in/details/z1",
        posted_at=None,
        posted_at_kind="unknown",
        raw=over.pop("raw", {"adzuna_created": iso(1)}),
        discovered_at=NOW,
        last_seen_at=NOW,
        **over,
    )
    db.add(job)
    db.commit()
    return job


def step(
    factory: sessionmaker[Session], plan: LeadsPlan, query: SearchQuery
) -> leads_service.LeadsOutcome:
    with factory() as db:
        run = AgentRun(task="t", run_type=RunType.DAILY, status=AgentRunStatus.RUNNING,
                       heartbeat_at=utcnow())  # fmt: skip
        db.add(run)
        db.commit()
        return run_leads_step(
            factory, db, RunLogger(db, run), plan, query, NOW - timedelta(hours=1), True
        )


def approve(factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, ref: str) -> None:
    monkeypatch.setattr(leads_cli, "get_session_factory", lambda: factory)
    assert leads_cli.main(["boards", "--approve", ref]) == 0


def safety(db: Session) -> tuple[int, int, int]:
    return tuple(  # type: ignore[return-value]
        db.scalar(select(func.count()).select_from(m)) or 0
        for m in (Application, BrowserSession, SubmissionAttempt)
    )


# --- configuration ---------------------------------------------------------------------------


def test_lead_resolution_is_off_by_default(sources_config: JobSourcesConfig) -> None:
    assert LeadResolutionConfig().enabled is False
    assert LeadResolutionConfig().auto_activate_verified is False
    assert LeadResolutionConfig().himalayas.enabled is False
    shipped = sources_config.lead_resolution
    assert shipped.enabled is False and shipped.auto_activate_verified is False
    assert shipped.himalayas.enabled is False
    assert build_daily_plan(get_settings(), config=sources_config).leads is None
    adzuna_cfg = sources_config.source("adzuna")
    assert adzuna_cfg is not None and adzuna_cfg.options["created_is_publication"] is False


def test_disabled_resolution_keeps_existing_dedupe_behavior(
    session_factory: sessionmaker[Session], query: SearchQuery, client: httpx.Client
) -> None:
    """Without lead rules, a Greenhouse posting matching an Adzuna row is folded into it as
    before (no new row, no lead tables touched)."""
    with session_factory() as db:
        aggregator = add_adzuna_row(db)
    gh = gh_source(client)
    with session_factory() as db:
        run = create_discovery_run(db, gh, query, dev_mode=True)
    DiscoveryService(session_factory).execute(run.id, gh, query)
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(JobLead)) == 0
        assert db.scalar(select(func.count()).select_from(EmployerBoard)) == 0
        row = db.get(Job, aggregator.id)
        assert row is not None and row.duplicate_of_id is None
        assert row.pipeline_state is JobPipelineState.DISCOVERED


# --- registration + timestamps ---------------------------------------------------------------


def test_adzuna_rows_become_leads_with_untrusted_timestamps_only(
    session_factory: sessionmaker[Session],
    query: SearchQuery,
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    world: World,
) -> None:
    with session_factory() as db:
        run = create_discovery_run(db, adzuna(client), query, dev_mode=True)
    DiscoveryService(session_factory).execute(run.id, adzuna(client), query)
    outcome = step(session_factory, leads_plan(sources_config, client), query)
    step(session_factory, leads_plan(sources_config, client), query)  # seen again: no new lead
    with session_factory() as db:
        lead = db.scalars(select(JobLead)).one()
        row = db.scalars(select(Job).where(Job.source == "adzuna")).one()
        assert lead.source == "adzuna" and lead.aggregator_job_id == row.id
        assert lead.aggregator_timestamp is not None and lead.aggregator_timestamp_trusted is False
        assert lead.times_seen == 2 and lead.first_seen_at <= lead.last_seen_at
        assert row.posted_at is None  # 'created' is never a post date
        assert compute_freshness(row.posted_at, utcnow(), 48) is Freshness.UNKNOWN
        assert lead.source_url == "https://www.adzuna.in/details/z1"  # no query string
    assert outcome.metrics["leads_registered"] == 1
    assert not any("/land/" in u for u in world.requests)  # Adzuna redirect_url never requested


def test_himalayas_leads_never_become_jobs_and_never_follow_links(
    session_factory: sessionmaker[Session],
    query: SearchQuery,
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    world: World,
) -> None:
    base = {
        "companyName": "Acme Tech",
        "companySlug": "acme-tech",
        "pubDate": int(NOW.timestamp()),
        "description": f"<p>{DESC}</p>",
        "applicationLink": "https://himalayas.app/companies/acme-tech/jobs/se/apply",
    }
    world.himalayas_items = [
        {**base, "title": "Software Engineer", "guid": "https://himalayas.app/companies/acme-tech/jobs/se?ref=x",
         "locationRestrictions": ["India"]},
        {**base, "title": "Software Engineer", "guid": "https://himalayas.app/j/worldwide",
         "locationRestrictions": []},  # worldwide is not remote-India
        {**base, "title": "Account Executive", "guid": "https://himalayas.app/j/ae",
         "locationRestrictions": ["India"]},  # not a target role
    ]  # fmt: skip
    plan = leads_plan(sources_config, client)
    plan.rules.himalayas.enabled = True
    plan.rules.himalayas.terms = ["Software Engineer"]
    outcome = step(session_factory, plan, query)
    with session_factory() as db:
        lead = db.scalars(select(JobLead)).one()
        assert lead.source == "himalayas" and lead.location == "Remote - India"
        assert lead.source_url == "https://himalayas.app/companies/acme-tech/jobs/se"
        assert lead.aggregator_timestamp is not None and not lead.aggregator_timestamp_trusted
        assert (
            db.scalar(select(func.count()).select_from(Job).where(Job.source == "himalayas")) == 0
        )
    assert outcome.metrics["leads_filtered"] == 1  # the non-target role
    assert not any("/apply" in u or "himalayas.app/companies" in u for u in world.requests)


def test_excluded_company_lead_is_rejected_and_never_probed(
    session_factory: sessionmaker[Session],
    query: SearchQuery,
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    world: World,
) -> None:
    with session_factory() as db:
        add_adzuna_row(db, company="Acme Labs AI Pvt Ltd")
    outcome = step(session_factory, leads_plan(sources_config, client), query)
    with session_factory() as db:
        assert db.scalars(select(JobLead)).one().status is LeadStatus.REJECTED
    assert outcome.metrics["requests_used"] == 0 and world.requests == []


# --- employer resolution ---------------------------------------------------------------------


def resolve(
    factory: sessionmaker[Session],
    cfg: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    company: str,
    **over: Any,
) -> tuple[JobLead, Any]:
    with factory() as db:
        row_id = add_adzuna_row(db, company=company).id
    outcome = step(factory, leads_plan(cfg, client, **over), query)
    with factory() as db:
        lead = db.scalars(select(JobLead).where(JobLead.aggregator_job_id == row_id)).one()
        db.expunge_all()
        return lead, outcome


def test_exact_board_name_is_verified_but_not_active(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
) -> None:
    lead, outcome = resolve(session_factory, sources_config, client, query, "Acme Tech Pvt Ltd")
    with session_factory() as db:
        board = db.scalars(select(EmployerBoard)).one()
        assert (board.platform, board.board_id, board.status) == (
            "greenhouse",
            "acmetech",
            BoardStatus.VERIFIED,
        )
        assert board.active is False and board.evidence["board_name"] == "Acme Tech"
    assert lead.status is LeadStatus.VERIFIED
    assert outcome.metrics["boards_crawled"] == 0  # not approved yet: never crawled
    assert outcome.metrics["boards_activated"] == 0


def test_weak_name_match_stays_candidate(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
) -> None:
    lead, _ = resolve(session_factory, sources_config, client, query, "Globex")
    assert lead.status is LeadStatus.BOARD_CANDIDATE
    with session_factory() as db:
        board = db.scalars(select(EmployerBoard).where(EmployerBoard.platform.is_not(None))).one()
        assert board.status is BoardStatus.CANDIDATE and board.evidence["rule"] == "board_name"


def test_lever_and_ashby_need_description_mentions(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
) -> None:
    initech, _ = resolve(session_factory, sources_config, client, query, "Initech")
    umbrella, _ = resolve(session_factory, sources_config, client, query, "Umbrella")
    assert initech.status is LeadStatus.VERIFIED  # 4/4 postings name the company
    assert umbrella.status is LeadStatus.BOARD_CANDIDATE  # 0/4
    with session_factory() as db:
        board = db.scalars(select(EmployerBoard).where(EmployerBoard.board_id == "umbrella")).one()
        assert board.evidence["ratio"] == 0.0 and board.evidence["threshold"] == 0.8


def test_short_board_ids_are_never_verified_automatically(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
) -> None:
    lead, _ = resolve(session_factory, sources_config, client, query, "AB")
    assert lead.status is LeadStatus.BOARD_CANDIDATE
    with session_factory() as db:
        board = db.scalars(select(EmployerBoard).where(EmployerBoard.board_id == "ab")).one()
        assert board.evidence["rule"] == "short_board_id"


def test_configured_boards_resolve_without_requests(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
) -> None:
    lead, outcome = resolve(session_factory, sources_config, client, query, "MongoDB")
    assert outcome.metrics["requests_used"] == 0 and outcome.metrics["boards_verified"] == 1
    # A configured board is crawled by normal discovery, so matching runs right away; with no
    # stored posting yet the lead is NO_MATCH (retried on later runs), never linked blindly.
    assert lead.status is LeadStatus.NO_MATCH and lead.matched_job_id is None
    with session_factory() as db:
        board = db.scalars(select(EmployerBoard)).one()
        assert board.configured and board.board_id == "mongodb" and not board.active
        assert board.status is BoardStatus.VERIFIED


def test_negative_cache(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    lead, first = resolve(session_factory, sources_config, client, query, "Nowhere Corp")
    assert lead.status is LeadStatus.NO_BOARD and first.metrics["requests_used"] > 0
    used = len(world.requests)
    _, second = resolve(
        session_factory, sources_config, client, query, "Nowhere Corp Private Limited"
    )
    assert second.metrics["negative_cache_hits"] == 1 and len(world.requests) == used
    with session_factory() as db:  # after 14 days it is checked again
        board = db.scalars(select(EmployerBoard)).one()
        assert board.status is BoardStatus.NOT_FOUND
        board.next_check_at = utcnow() - timedelta(seconds=1)
        db.commit()
    resolve(session_factory, sources_config, client, query, "Nowhere Corp Ltd")
    assert len(world.requests) > used


def test_request_budget_is_enforced(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    with session_factory() as db:
        for name in ("Alpha One", "Beta Two", "Gamma Three"):
            add_adzuna_row(db, company=name, sid=name)
    outcome = step(
        session_factory, leads_plan(sources_config, client, max_requests_per_run=3), query
    )
    assert outcome.metrics["requests_used"] == 3 == len(world.requests)
    assert outcome.metrics["budget_exhausted"] == 1
    with session_factory() as db:
        statuses = set(db.scalars(select(JobLead.status)).all())
    assert LeadStatus.NEW in statuses  # left for the next run, not negative-cached


def test_429_stops_that_platform_for_the_run(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    world.sr_status = 429
    with session_factory() as db:
        for name in ("Nowhere One", "Nowhere Two"):
            add_adzuna_row(db, company=name, sid=name)
    outcome = step(session_factory, leads_plan(sources_config, client), query)
    sr_calls = [u for u in world.requests if "smartrecruiters" in u]
    assert len(sr_calls) == 1 and outcome.metrics["rate_limit_stops"] == 1


# --- crawl + match + dedupe --------------------------------------------------------------------


def test_inactive_boards_are_never_crawled(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    resolve(session_factory, sources_config, client, query, "Acme Tech Pvt Ltd")
    step(session_factory, leads_plan(sources_config, client), query)
    assert not any(u.endswith("/acmetech/jobs") for u in world.requests)
    with session_factory() as db:
        assert (
            db.scalar(select(func.count()).select_from(Job).where(Job.source == "greenhouse")) == 0
        )


def test_approved_board_is_crawled_matched_and_linked(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lead, _ = resolve(session_factory, sources_config, client, query, "Acme Tech Pvt Ltd")
    approve(session_factory, monkeypatch, "greenhouse:acmetech")
    outcome = step(session_factory, leads_plan(sources_config, client), query)
    with session_factory() as db:
        lead_row = db.get(JobLead, lead.id)
        assert lead_row is not None and lead_row.status is LeadStatus.MATCHED
        trusted = db.get(Job, lead_row.matched_job_id)
        assert trusted is not None and trusted.source == "greenhouse"
        # Freshness comes from the ATS adapter (first_published 30h ago) - not from Adzuna.
        assert trusted.posted_at is not None and trusted.posted_at_kind == "published"
        assert abs((trusted.posted_at - (NOW - timedelta(hours=30))).total_seconds()) < 1
        assert compute_freshness(trusted.posted_at, utcnow(), 48) is Freshness.FRESH
        aggregator = db.get(Job, lead_row.aggregator_job_id)
        assert aggregator is not None and aggregator.posted_at is None  # still no post date
        assert aggregator.duplicate_of_id == trusted.id
        assert aggregator.pipeline_state is JobPipelineState.DUPLICATE
        assert any(a.get("source") == "adzuna" and a.get("timestamp_trusted") is False
                   for a in trusted.source_attributions or [])  # fmt: skip
        # The stale Data Engineer posting on the same board is stored too - and it is STALE.
        stale = db.scalars(select(Job).where(Job.title == "Data Engineer")).one()
        assert compute_freshness(stale.posted_at, utcnow(), 48) is Freshness.STALE
    assert outcome.metrics["leads_matched"] == 1 and outcome.metrics["trusted_jobs_created"] == 2


def test_fresh_looking_lead_never_makes_an_old_posting_fresh(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with session_factory() as db:  # aggregator: "posted an hour ago"; ATS: four months ago
        add_adzuna_row(
            db, title="Data Engineer", location="Pune, Maharashtra, India", description=None
        )
    step(session_factory, leads_plan(sources_config, client), query)
    approve(session_factory, monkeypatch, "greenhouse:acmetech")
    step(session_factory, leads_plan(sources_config, client), query)
    with session_factory() as db:
        lead = db.scalars(select(JobLead)).one()
        trusted = db.get(Job, lead.matched_job_id)
        assert lead.status is LeadStatus.MATCHED and trusted is not None
        assert compute_freshness(trusted.posted_at, utcnow(), 48) is Freshness.STALE


def test_existing_trusted_job_is_linked_not_duplicated(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = gh_source(client)
    with session_factory() as db:
        run = create_discovery_run(db, gh, query, dev_mode=True)
    DiscoveryService(session_factory).execute(run.id, gh, query)  # the ATS job already exists
    lead, _ = resolve(session_factory, sources_config, client, query, "Acme Tech Pvt Ltd")
    approve(session_factory, monkeypatch, "greenhouse:acmetech")
    outcome = step(session_factory, leads_plan(sources_config, client), query)
    with session_factory() as db:
        lead_row = db.get(JobLead, lead.id)
        assert lead_row is not None and lead_row.status is LeadStatus.DUPLICATE
        posting = db.scalars(select(Job).where(Job.source_job_id == "acmetech:101")).all()
        assert len(posting) == 1 and lead_row.matched_job_id == posting[0].id
    assert (
        outcome.metrics["existing_jobs_linked"] == 1
        and outcome.metrics["trusted_jobs_created"] == 0
    )


def test_rejected_jobs_stay_rejected(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lead, _ = resolve(session_factory, sources_config, client, query, "Acme Tech Pvt Ltd")
    with session_factory() as db:
        aggregator = db.get(Job, lead.aggregator_job_id)
        assert aggregator is not None
        aggregator.pipeline_state = JobPipelineState.REJECTED_BY_AGENT  # e.g. UNKNOWN freshness
        db.commit()
    approve(session_factory, monkeypatch, "greenhouse:acmetech")
    step(session_factory, leads_plan(sources_config, client), query)
    with session_factory() as db:
        aggregator = db.get(Job, lead.aggregator_job_id)
        assert (
            aggregator is not None
            and aggregator.pipeline_state is JobPipelineState.REJECTED_BY_AGENT
        )
        assert aggregator.duplicate_of_id is not None  # linked, not resurrected


def test_strict_match_failure_leaves_the_lead_unlinked(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with session_factory() as db:  # same company and title, but in Hyderabad
        add_adzuna_row(db, location="Hyderabad, Telangana, India")
    step(session_factory, leads_plan(sources_config, client), query)
    approve(session_factory, monkeypatch, "greenhouse:acmetech")
    step(session_factory, leads_plan(sources_config, client), query)
    with session_factory() as db:
        lead = db.scalars(select(JobLead)).one()
        assert lead.status is LeadStatus.NO_MATCH and lead.matched_job_id is None
        aggregator = db.get(Job, lead.aggregator_job_id)
        assert aggregator is not None and aggregator.duplicate_of_id is None
        assert aggregator.pipeline_state is JobPipelineState.DISCOVERED


# --- the daily run: one canonical job, one analysis ---------------------------------------------


@pytest.fixture
def daily_plan(
    profile: UserProfile, sources_config: JobSourcesConfig, client: httpx.Client
) -> Callable[..., DailyPlan]:
    def make(**over: Any) -> DailyPlan:
        heuristic = HeuristicAnalyzer(
            sources_config.search.location_patterns, sources_config.search.term_aliases
        )
        return DailyPlan(
            sources=["adzuna"],
            build_source=lambda name: adzuna(client),
            query=sources_config.build_query([], [], excluded_companies=EXCLUDED, max_age_hours=48),
            analyzer=lambda: heuristic,
            profile=profile,
            rules=load_shortlist_rules(get_settings().shortlist_rules_path),
            dev_mode=True,
            analysis_limit=50,
            leads=leads_plan(sources_config, client, **over),
        )

    return make


def test_daily_run_resolves_leads_and_analyzes_the_trusted_job_once(
    session_factory: sessionmaker[Session],
    daily_plan: Callable[..., DailyPlan],
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    _, first = run_daily(session_factory, daily_plan(), "manual")
    assert first["leads"]["leads_registered"] == 1 and first["leads"]["boards_verified"] == 1
    assert first["leads"]["boards_crawled"] == 0 and first["counts"]["analyzed"] == 0
    approve(session_factory, monkeypatch, "greenhouse:acmetech")
    _, second = run_daily(session_factory, daily_plan(), "manual")
    _, third = run_daily(session_factory, daily_plan(), "manual")

    assert second["leads"]["leads_matched"] == 1 and second["leads"]["trusted_jobs_created"] == 2
    assert second["counts"]["new_fresh_jobs"] == 1  # the stale Data Engineer is new but not fresh
    assert second["counts"]["analyzed"] == 1 and second["leads"]["claude_analyses_triggered"] == 1
    assert third["counts"]["analyzed"] == 0 and third["leads"]["trusted_jobs_created"] == 0
    with session_factory() as db:
        trusted = db.scalars(select(Job).where(Job.source_job_id == "acmetech:101")).all()
        assert len(trusted) == 1  # one canonical job
        analyses = db.scalars(select(JobAnalysis)).all()
        assert [a.job_id for a in analyses] == [trusted[0].id]  # exactly one analysis, of it
        assert analyses[0].freshness is Freshness.FRESH
        aggregator = db.scalars(select(Job).where(Job.source == "adzuna")).one()
        assert aggregator.posted_at is None and aggregator.duplicate_of_id == trusted[0].id
        assert safety(db) == (0, 0, 0)
        # Credentials never persisted anywhere, nor logged.
        dumps = [
            *(str(v) for lead in db.scalars(select(JobLead)) for v in vars(lead).values()),
            *(str(b.evidence) for b in db.scalars(select(EmployerBoard))),
            *(f"{a.input}{a.output}{a.error}" for a in db.scalars(select(AgentAction))),
            *(str(j.url) + str(j.source_attributions) for j in db.scalars(select(Job))),
        ]
    blob = " ".join(dumps) + caplog.text + str(first) + str(second)
    assert APP_ID not in blob and APP_KEY not in blob
    assert not any("/land/" in u for u in world.requests)
    assert all(u.split("/")[2] in {"api.adzuna.com", "boards-api.greenhouse.io", "api.lever.co",
                                   "api.ashbyhq.com", "api.smartrecruiters.com"}
               for u in world.requests)  # fmt: skip


def test_cli_lists_leads_and_rejects_boards(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    resolve(session_factory, sources_config, client, query, "Acme Tech Pvt Ltd")
    monkeypatch.setattr(leads_cli, "get_session_factory", lambda: session_factory)
    assert leads_cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "VERIFIED" in out and "greenhouse:acmetech" in out and "untrusted" in out
    assert leads_cli.main(["boards"]) == 0
    assert "inactive" in capsys.readouterr().out
    assert leads_cli.main(["boards", "--reject", "greenhouse:acmetech"]) == 0
    assert leads_cli.main(["boards", "--approve", "greenhouse:nosuch"]) == 1
    with session_factory() as db:
        board = db.scalars(select(EmployerBoard)).one()
        assert board.status is BoardStatus.REJECTED and not board.active
        assert db.scalars(select(JobLead)).one().status is LeadStatus.NO_BOARD


@pytest.fixture(autouse=True)
def _no_application_side_effects(session_factory: sessionmaker[Session]) -> Iterator[None]:
    yield
    with session_factory() as db:
        assert safety(db) == (0, 0, 0)


def test_himalayas_lead_resolves_to_the_ats_date_never_pubdate(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world.gh_boards["acmetech"]["jobs"].append(
        {
            "id": 103,
            "title": "Backend Engineer",
            "company_name": "Acme Tech",
            "absolute_url": "https://job-boards.greenhouse.io/acmetech/jobs/103",
            "location": {"name": "Remote - India"},
            "first_published": iso(20),
            "content": f"&lt;p&gt;{DESC}&lt;/p&gt;",
        }
    )
    world.himalayas_items = [
        {
            "title": "Backend Engineer",
            "companyName": "Acme Tech",
            "companySlug": "acmetech",
            "guid": "https://himalayas.app/companies/acmetech/jobs/software-engineer",
            "locationRestrictions": ["India"],
            "description": f"<p>{DESC}</p>",
            "pubDate": int(NOW.timestamp()),  # "just now" on Himalayas
        }
    ]
    plan = leads_plan(sources_config, client)
    plan.rules.himalayas.enabled = True
    plan.rules.himalayas.terms = ["Backend Engineer"]
    step(session_factory, plan, query)  # registered + board verified (not active)
    approve(session_factory, monkeypatch, "greenhouse:acmetech")
    step(session_factory, plan, query)
    with session_factory() as db:
        lead = db.scalars(select(JobLead)).one()
        trusted = db.get(Job, lead.matched_job_id)
        assert lead.status is LeadStatus.MATCHED and trusted is not None
        assert lead.aggregator_timestamp is not None
        assert trusted.posted_at is not None and trusted.posted_at != lead.aggregator_timestamp
        assert abs((trusted.posted_at - (NOW - timedelta(hours=20))).total_seconds()) < 1
        # Remote-India lead, remote-India posting: same title, same region, same description.
        assert trusted.source_job_id == "acmetech:103" and lead.evidence["location"] == "region"


def test_auto_activation_is_opt_in(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
) -> None:
    _, outcome = resolve(
        session_factory, sources_config, client, query, "Acme Tech Pvt Ltd",
        auto_activate_verified=True,
    )  # fmt: skip
    assert outcome.metrics["boards_activated"] == 1 and outcome.metrics["boards_crawled"] == 1
    assert outcome.metrics["leads_matched"] == 1
