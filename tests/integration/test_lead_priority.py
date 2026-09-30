# ruff: noqa: F811 - test functions take the imported shared fixtures as parameters.
"""Phase 5.7B: lead-resolution efficiency - queue order, request budgets, caches.

Priority only decides which company is resolved first. These tests also prove it changes
nothing about eligibility: timestamps stay untrusted, verification and matching rules are
the same, nothing is activated automatically. All HTTP is mocked and every request recorded."""

from datetime import timedelta
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.services.analysis import select_jobs_for_daily
from backend.services.daily import run_daily
from backend.services.freshness import compute_freshness
from backend.services.leads import (
    TIER_FREE,
    TIER_NEW,
    TIER_OVERRIDE,
    TIER_RECHECK,
    board_id_guesses,
    configured_boards,
    resolution_queue,
    trusted_board_index,
)
from database.base import utcnow
from database.enums import BoardStatus, Freshness, LeadStatus
from database.models import EmployerBoard, Job, JobAnalysis, JobLead
from database.repositories import CompanyRepository
from integrations.job_sources.base import SearchQuery
from integrations.job_sources.config import (
    BoardOverride,
    JobSourcesConfig,
    LeadResolutionConfig,
)

# Shared fake network + helpers (fixtures are re-exported so pytest registers them here).
from tests.integration.test_leads import (  # noqa: F401
    NOW,
    World,
    _no_application_side_effects,
    add_adzuna_row,
    approve,
    client,
    daily_plan,
    leads_plan,
    mocked_crawler,
    query,
    step,
    world,
)


def rows(factory: sessionmaker[Session], *companies: str, **over: Any) -> None:
    with factory() as db:
        for i, name in enumerate(companies):
            add_adzuna_row(db, company=name, sid=f"{name}-{i}", **over)


def requests_to(world: World, host: str) -> int:
    return sum(1 for u in world.requests if host in u)


def queue_order(factory: sessionmaker[Session], cfg: JobSourcesConfig, **rules: Any) -> list[str]:
    """Register the Adzuna rows as leads (no resolution) and return the company order."""
    from backend.services.leads import adzuna_lead_records, register_leads

    lr = LeadResolutionConfig(enabled=True, **rules)
    with factory() as db:
        q = cfg.build_query([], [])
        register_leads(
            db,
            adzuna_lead_records(db, NOW - timedelta(days=1)),
            q,
            {
                k: 0
                for k in (
                    "leads_discovered",
                    "leads_filtered",
                    "leads_already_known",
                    "leads_registered",
                    "leads_rejected",
                )
            },
        )
        db.commit()
        leads = db.scalars(select(JobLead).where(JobLead.status == LeadStatus.NEW)).all()
        queue = resolution_queue(
            db, list(leads), lr, configured_boards(cfg), trusted_board_index(db)
        )
        return [f"{item.tier}:{item.normalized}:{len(item.leads)}" for item in queue]


# --- 1-4, 7-8: deterministic priority ------------------------------------------------------------


def test_queue_order_is_deterministic_and_documented(
    session_factory: sessionmaker[Session], sources_config: JobSourcesConfig
) -> None:
    with session_factory() as db:
        for i in range(3):
            add_adzuna_row(db, company="Busy Corp Alpha", sid=f"busy-{i}")
        add_adzuna_row(db, company="Zeta Solo Works", sid="solo-z")
        add_adzuna_row(db, company="Alpha Solo Works", sid="solo-a")
        add_adzuna_row(db, company="MongoDB", sid="mongo")  # configured board: free
        db.add(
            EmployerBoard(
                normalized_company="expired nothing",
                company_name="Expired Nothing",
                status=BoardStatus.NOT_FOUND,
                next_check_at=utcnow() - timedelta(days=1),
            )
        )
        add_adzuna_row(db, company="Expired Nothing", sid="expired")
        add_adzuna_row(db, company="Globex Override", sid="glx")
        db.commit()
    order = queue_order(
        session_factory,
        sources_config,
        overrides={"Globex Override": BoardOverride(platform="greenhouse", board="acmetech")},
    )
    assert order == [
        f"{TIER_FREE}:mongodb:1",  # configured: no request at all
        f"{TIER_OVERRIDE}:globex override:1",  # explicit override: one request
        f"{TIER_NEW}:busy corp alpha:3",  # most pending leads first
        f"{TIER_NEW}:alpha solo works:1",  # then by name (same seen time, same hint)
        f"{TIER_NEW}:zeta solo works:1",
        f"{TIER_RECHECK}:expired nothing:1",  # checked before, cache expired: last
    ]
    again = queue_order(
        session_factory,
        sources_config,
        overrides={"Globex Override": BoardOverride(platform="greenhouse", board="acmetech")},
    )
    assert again == order  # deterministic: same input, same order


def test_aggregator_dates_never_change_the_order_or_freshness(
    session_factory: sessionmaker[Session], sources_config: JobSourcesConfig
) -> None:
    """Phase 5.7C: the (untrusted) aggregator date is not part of the queue key at all."""
    with session_factory() as db:
        ids = [
            add_adzuna_row(db, company=name, sid=name, raw={"adzuna_created": created}).id
            for name, created in [
                ("Old Hint Co", (NOW - timedelta(days=9)).isoformat()),
                ("New Hint Co", (NOW - timedelta(hours=1)).isoformat()),
                ("No Hint Co", None),
            ]
        ]
    before = queue_order(session_factory, sources_config)
    assert [o.split(":")[1] for o in before] == ["new hint", "no hint", "old hint"]  # by name
    with session_factory() as db:  # reverse every aggregator date
        for i, lead in enumerate(db.scalars(select(JobLead).order_by(JobLead.company_name))):
            lead.aggregator_timestamp = NOW - timedelta(days=30 * i) if i % 2 else NOW
        db.commit()
    assert queue_order(session_factory, sources_config) == before
    with session_factory() as db:
        for job_id in ids:  # and nothing about the jobs changed
            job = db.get(Job, job_id)
            assert job is not None and job.posted_at is None
            assert compute_freshness(job.posted_at, utcnow(), 48) is Freshness.UNKNOWN
        assert not db.scalar(
            select(func.count()).select_from(JobLead).where(JobLead.aggregator_timestamp_trusted)
        )


# --- 2, 4, 9: known boards cost nothing ----------------------------------------------------------


def test_configured_and_previously_verified_boards_need_no_requests(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    with session_factory() as db:
        db.add(
            EmployerBoard(
                normalized_company="acme tech",
                company_name="Acme Tech",
                platform="greenhouse",
                board_id="acmetech",
                status=BoardStatus.VERIFIED,
                active=False,
            )
        )
        db.commit()
    rows(session_factory, "Bitwarden", "Acme Tech Pvt Ltd")
    outcome = step(session_factory, leads_plan(sources_config, client), query)
    assert world.requests == [] and outcome.metrics["requests_used"] == 0
    assert outcome.metrics["companies_resolved_free"] == 2
    assert outcome.metrics["companies_probed"] == 0
    with session_factory() as db:
        boards = {b.board_id: b for b in db.scalars(select(EmployerBoard))}
        assert boards["bitwarden"].evidence == {"rule": "configured_board"}
        assert boards["acmetech"].status is BoardStatus.VERIFIED and not boards["acmetech"].active


def test_stored_trusted_jobs_map_a_company_to_its_configured_board(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    """'Bosch Group' is configured as SmartRecruiters 'BoschGroup' named 'Bosch'; the stored
    trusted postings of that board prove the mapping - no guessing, no request."""
    with session_factory() as db:
        db.add(
            Job(
                source="smartrecruiters",
                source_job_id="BoschGroup:1",
                company=CompanyRepository(db).get_or_create("Bosch Group"),
                title="Software Engineer",
                url="https://jobs.smartrecruiters.com/BoschGroup/1",
                posted_at=NOW - timedelta(days=3),
                posted_at_kind="released",
            )
        )
        db.commit()
    rows(session_factory, "Bosch Group")
    outcome = step(session_factory, leads_plan(sources_config, client), query)
    assert world.requests == []
    with session_factory() as db:
        board = db.scalars(select(EmployerBoard)).one()
        assert (board.platform, board.board_id) == ("smartrecruiters", "BoschGroup")
        assert board.configured and board.evidence == {"rule": "trusted_ats_jobs"}
    assert outcome.metrics["boards_verified"] == 1


def test_override_is_used_first_and_costs_one_request(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    rows(session_factory, "Acme Holdings")  # name would never guess 'acmetech'
    plan = leads_plan(
        sources_config,
        client,
        overrides={"Acme Holdings": BoardOverride(platform="greenhouse", board="acmetech")},
    )
    step(session_factory, plan, query)
    assert world.requests == ["https://boards-api.greenhouse.io/v1/boards/acmetech"]
    with session_factory() as db:
        board = db.scalars(select(EmployerBoard)).one()
        assert board.status is BoardStatus.VERIFIED and board.evidence["rule"] == "config_override"
        assert board.active is False  # verified is not approved


# --- 5, 13: one resolution per company -----------------------------------------------------------


def test_many_leads_from_one_company_are_resolved_once(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    rows(session_factory, *["Nowhere Industries"] * 10)
    outcome = step(session_factory, leads_plan(sources_config, client), query)
    # guesses 'nowhereindustries', 'nowhere-industries' (x 4 platforms) - once, not x 10
    assert len(world.requests) == 8 == len(set(world.requests))
    assert outcome.metrics["companies_probed"] == 1 and outcome.metrics["leads_no_board"] == 10
    with session_factory() as db:
        leads = db.scalars(select(JobLead)).all()
        assert len(leads) == 10 and {lead.attempts for lead in leads} == {1}
        assert db.scalar(select(func.count()).select_from(EmployerBoard)) == 1
    again = step(session_factory, leads_plan(sources_config, client), query)
    assert again.metrics["requests_used"] == 0 and len(world.requests) == 8  # nothing pending


def test_equivalent_guesses_are_not_probed_twice() -> None:
    assert board_id_guesses("amazon", None) == ["amazon"]  # compact == hyphenated
    assert board_id_guesses("acme tech", "acmetech") == ["acmetech", "acme-tech", "acme"]


def test_same_board_id_is_probed_once_per_run_across_companies(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    rows(session_factory, "Nowhere Solutions", "Nowhere Systems")  # both also guess 'nowhere'
    plan = leads_plan(sources_config, client, max_requests_per_company=12)
    outcome = step(session_factory, plan, query)
    assert len(world.requests) == len(set(world.requests))
    assert outcome.metrics["probe_cache_hits"] == 4  # 'nowhere' x 4 platforms, answered once


# --- 6: negative cache ---------------------------------------------------------------------------


def test_negative_cache_is_checked_before_any_probe(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    rows(session_factory, "Nowhere Industries")
    step(session_factory, leads_plan(sources_config, client), query)
    first = len(world.requests)
    assert first > 0
    rows(session_factory, "Nowhere Industries Pvt Ltd")  # a later lead, same company
    later = step(session_factory, leads_plan(sources_config, client), query)
    assert len(world.requests) == first and later.metrics["negative_cache_hits"] == 1
    with session_factory() as db:
        board = db.scalars(select(EmployerBoard)).one()
        assert board.next_check_at is not None  # never permanent
        board.next_check_at = utcnow() - timedelta(seconds=1)
        db.commit()
    rows(session_factory, "Nowhere Industries Ltd")
    step(session_factory, leads_plan(sources_config, client), query)
    assert len(world.requests) > first


# --- 10-12: budgets and 429 ----------------------------------------------------------------------


def test_per_company_budget_caps_one_company_and_the_run_goes_on(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    rows(session_factory, "Nowhere Industries", "Initech")
    outcome = step(
        session_factory, leads_plan(sources_config, client, max_requests_per_company=2), query
    )
    with session_factory() as db:
        nowhere = db.scalars(
            select(EmployerBoard).where(EmployerBoard.normalized_company == "nowhere industries")
        ).one()
        assert nowhere.evidence["complete"] is False and len(nowhere.evidence["tried"]) == 2
        initech = db.scalars(select(JobLead).where(JobLead.company_name == "Initech")).one()
    # Initech is still resolved within its own 2 requests: greenhouse, smartrecruiters miss
    # -> NO_BOARD (Ashby would have verified it, but the cap is the cap).
    assert initech.status is LeadStatus.NO_BOARD
    assert outcome.metrics["requests_used"] == 4 and outcome.metrics["companies_probed"] == 2


def test_global_budget_defers_the_rest_without_negative_caching(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    rows(session_factory, "Nowhere Industries", "Other Place", "Third Thing")
    outcome = step(
        session_factory, leads_plan(sources_config, client, max_requests_per_run=10), query
    )
    assert outcome.metrics["requests_used"] == 10 == len(world.requests)
    assert outcome.metrics["budget_exhausted"] == 1 and outcome.metrics["companies_deferred"] == 2
    with session_factory() as db:
        statuses = {lead.company_name: lead.status for lead in db.scalars(select(JobLead))}
        assert db.scalar(select(func.count()).select_from(EmployerBoard)) == 1  # only the first
    assert sorted(s.value for s in statuses.values()) == ["NEW", "NEW", "NO_BOARD"]


def test_company_cap_counts_only_companies_that_need_requests(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    rows(session_factory, "Bitwarden", "MongoDB", "Nowhere Industries", "Other Place")
    outcome = step(
        session_factory, leads_plan(sources_config, client, max_companies_per_run=1), query
    )
    assert outcome.metrics["companies_resolved_free"] == 2  # configured: always resolved
    assert outcome.metrics["companies_probed"] == 1 and outcome.metrics["companies_deferred"] == 1


def test_429_still_stops_that_platform_for_every_later_company(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    world.sr_status = 429
    rows(session_factory, "Nowhere Industries", "Other Place", "Third Thing")
    outcome = step(session_factory, leads_plan(sources_config, client), query)
    assert requests_to(world, "smartrecruiters") == 1 and outcome.metrics["rate_limit_stops"] == 1
    with session_factory() as db:
        board = db.scalars(
            select(EmployerBoard).where(EmployerBoard.normalized_company == "nowhere industries")
        ).one()
        assert board.evidence["stopped_platforms"] == ["smartrecruiters"]


# --- 14-19: no duplicates, exclusions, crawling, defaults ----------------------------------------


def test_two_leads_for_one_posting_give_one_job_and_one_analysis(
    session_factory: sessionmaker[Session],
    daily_plan: Any,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
) -> None:
    rows(session_factory, "Acme Tech")  # a second, pre-existing Adzuna row for the same job
    run_daily(session_factory, daily_plan(), "manual")
    approve(session_factory, monkeypatch, "greenhouse:acmetech")
    run_daily(session_factory, daily_plan(), "manual")
    run_daily(session_factory, daily_plan(), "manual")
    with session_factory() as db:
        trusted = db.scalars(select(Job).where(Job.source_job_id == "acmetech:101")).all()
        assert len(trusted) == 1
        linked = {lead.matched_job_id for lead in db.scalars(select(JobLead))}
        assert linked == {trusted[0].id}  # both leads -> the same canonical job
        assert [a.job_id for a in db.scalars(select(JobAnalysis))] == [trusted[0].id]
        assert select_jobs_for_daily(db, utcnow(), 48, 50) == []


def test_excluded_companies_never_enter_the_queue(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    rows(session_factory, "Acme Labs AI", "Acme Labs.ai", "Acme Labs AI Pvt Ltd")
    outcome = step(session_factory, leads_plan(sources_config, client), query)
    assert world.requests == [] and outcome.metrics["companies_pending"] == 0
    assert outcome.metrics["leads_rejected"] == 3


def test_priority_never_activates_or_crawls(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    rows(session_factory, "Acme Tech", "Acme Tech", "Acme Tech")  # top priority, verifiable
    outcome = step(session_factory, leads_plan(sources_config, client), query)
    assert outcome.metrics["boards_verified"] == 1 and outcome.metrics["boards_activated"] == 0
    assert outcome.metrics["boards_crawled"] == 0
    assert not any(u.endswith("/acmetech/jobs") for u in world.requests)


def test_defaults_stay_safe(sources_config: JobSourcesConfig) -> None:
    default = LeadResolutionConfig()
    assert default.enabled is False and default.auto_activate_verified is False
    assert default.max_requests_per_company == 8 and default.max_requests_per_run == 60
    shipped = sources_config.lead_resolution
    assert shipped.enabled is False and shipped.auto_activate_verified is False
    assert shipped.max_requests_per_company == 8 and shipped.himalayas.enabled is False
