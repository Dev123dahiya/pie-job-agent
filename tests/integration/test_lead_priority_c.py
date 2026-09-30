# ruff: noqa: F811 - test functions take the imported shared fixtures as parameters.
"""Phase 5.7C: evidence-based resolution priority, explicit ATS references, miss reuse.

Order only - never eligibility: cost before lead count, lead count bucketed (1/2/3+),
aggregator dates not in the key. All HTTP is mocked; every request is recorded."""

from datetime import timedelta
from typing import Any

import httpx2 as httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.services.leads import (
    LEAD_BUCKET_CAP,
    TIER_FREE,
    TIER_NEW,
    TIER_REFERENCE,
    configured_boards,
    known_misses,
    resolution_queue,
    trusted_board_index,
)
from database.base import utcnow
from database.enums import BoardStatus, LeadStatus
from database.models import EmployerBoard, JobLead
from integrations.job_sources.base import SearchQuery
from integrations.job_sources.config import JobSourcesConfig, LeadResolutionConfig
from tests.integration.test_leads import (  # noqa: F401
    DESC,
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


def leads_for(factory: sessionmaker[Session], company: str, n: int, **over: Any) -> None:
    with factory() as db:
        for i in range(n):
            add_adzuna_row(db, company=company, sid=f"{company}-{i}", **over)


def order(factory: sessionmaker[Session], cfg: JobSourcesConfig) -> list[tuple[str, int, int]]:
    """Register pending Adzuna rows as leads; return [(company, tier, cost)] in queue order."""
    from backend.services.leads import adzuna_lead_records, register_leads

    with factory() as db:
        metrics = {k: 0 for k in ("leads_discovered", "leads_filtered", "leads_already_known",
                                  "leads_registered", "leads_rejected")}  # fmt: skip
        register_leads(db, adzuna_lead_records(db, NOW - timedelta(days=1)),
                       cfg.build_query([], []), metrics)  # fmt: skip
        db.commit()
        leads = db.scalars(select(JobLead).where(JobLead.status == LeadStatus.NEW)).all()
        queue = resolution_queue(
            db, list(leads), LeadResolutionConfig(enabled=True), configured_boards(cfg),
            trusted_board_index(db), known_misses(db),
        )  # fmt: skip
        return [(i.normalized, i.tier, i.cost) for i in queue]


# --- 1-5: cost first, bucketed lead count, recency, name -----------------------------------------


def test_one_guess_company_outranks_two_guess_company_whatever_the_lead_count(
    session_factory: sessionmaker[Session], sources_config: JobSourcesConfig
) -> None:
    leads_for(session_factory, "Busy Traders", 30)  # 2 guesses -> 8 requests
    leads_for(session_factory, "Solo", 1)  # 1 guess  -> 4 requests
    assert order(session_factory, sources_config) == [
        ("solo", TIER_NEW, 4),
        ("busy traders", TIER_NEW, 8),
    ]


def test_lead_counts_are_bucketed_3_and_30_tie(
    session_factory: sessionmaker[Session], sources_config: JobSourcesConfig
) -> None:
    assert LEAD_BUCKET_CAP == 3
    leads_for(session_factory, "Zulu Many", 30)
    leads_for(session_factory, "Alpha Three", 3)
    leads_for(session_factory, "Mike Two", 2)
    # Same cost (8), same recency: 3 and 30 share the top bucket -> name decides; 2 is below.
    assert [c for c, _, _ in order(session_factory, sources_config)] == [
        "alpha three",
        "zulu many",
        "mike two",
    ]


def test_recency_breaks_equal_buckets_then_name(
    session_factory: sessionmaker[Session], sources_config: JobSourcesConfig
) -> None:
    leads_for(session_factory, "Alpha Older", 1)
    leads_for(session_factory, "Zulu Newer", 1)
    leads_for(session_factory, "Bravo Older", 1)
    order(session_factory, sources_config)  # registers the leads
    with session_factory() as db:
        for lead in db.scalars(select(JobLead)):
            lead.last_seen_at = NOW - timedelta(days=2)
            if lead.company_name == "Zulu Newer":
                lead.last_seen_at = NOW  # seen most recently by PIE
        db.commit()
    with session_factory() as db:
        leads = db.scalars(select(JobLead).where(JobLead.status == LeadStatus.NEW)).all()
        queue = resolution_queue(db, list(leads), LeadResolutionConfig(enabled=True),
                                 configured_boards(sources_config), {})  # fmt: skip
    assert [i.normalized for i in queue] == ["zulu newer", "alpha older", "bravo older"]


def test_free_companies_come_first_and_cost_nothing(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    leads_for(session_factory, "Bitwarden", 1)  # configured board
    leads_for(session_factory, "Nowhere Industries", 1)  # needs the full 8
    assert order(session_factory, sources_config)[0] == ("bitwarden", TIER_FREE, 0)
    outcome = step(session_factory, leads_plan(sources_config, client), query)
    # the free one used nothing; the other still got its whole per-company cap of 8
    assert outcome.metrics["companies_resolved_free"] == 1
    assert outcome.metrics["requests_used"] == 8 == len(world.requests)


# --- 9-12: explicit ATS references ---------------------------------------------------------------


def ref_desc(board: str, job: int = 101) -> str:
    return f"{DESC} Apply: https://boards.greenhouse.io/{board}/jobs/{job}"


def test_explicit_reference_costs_one_request_and_is_still_verified(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    leads_for(session_factory, "Acme Tech", 1, description=ref_desc("acmetech"))
    assert order(session_factory, sources_config) == [("acme tech", TIER_REFERENCE, 1)]
    outcome = step(session_factory, leads_plan(sources_config, client), query)
    assert world.requests == ["https://boards-api.greenhouse.io/v1/boards/acmetech"]
    assert outcome.metrics["explicit_references"] == 1
    with session_factory() as db:
        board = db.scalars(select(EmployerBoard)).one()
        assert board.status is BoardStatus.VERIFIED and not board.active
        assert board.evidence["rule"] == "board_name"  # the normal identity proof
        assert board.evidence["found_via"] == "explicit_ats_reference"


def test_explicit_reference_without_identity_proof_is_not_verified(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
) -> None:
    # A recruiter's lead for "Initrode Staffing" quoting Acme Tech's posting.
    leads_for(session_factory, "Initrode Staffing", 1, description=ref_desc("acmetech"))
    step(session_factory, leads_plan(sources_config, client), query)
    with session_factory() as db:
        lead = db.scalars(select(JobLead)).one()
        assert lead.status is LeadStatus.BOARD_CANDIDATE  # name mismatch: never VERIFIED
        board = db.get(EmployerBoard, lead.employer_board_id)
        assert board is not None and board.status is BoardStatus.CANDIDATE


def test_another_companys_board_is_never_reused(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
) -> None:
    with session_factory() as db:  # acmetech is Acme Tech's verified board
        db.add(EmployerBoard(normalized_company="acme tech", company_name="Acme Tech",
                             platform="greenhouse", board_id="acmetech",
                             status=BoardStatus.VERIFIED))  # fmt: skip
        db.commit()
    leads_for(session_factory, "Globex", 1, description=ref_desc("acmetech"))
    step(session_factory, leads_plan(sources_config, client), query)
    with session_factory() as db:
        lead = db.scalars(select(JobLead).where(JobLead.company_name == "Globex")).one()
        board = db.get(EmployerBoard, lead.employer_board_id) if lead.employer_board_id else None
        assert board is None or board.board_id != "acmetech"
        assert lead.status is not LeadStatus.VERIFIED
        acme = db.scalars(select(EmployerBoard).where(EmployerBoard.board_id == "acmetech")).one()
        assert acme.normalized_company == "acme tech"  # untouched


def test_a_missing_referenced_board_is_not_probed_again(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    leads_for(session_factory, "Ghost One", 1, description=ref_desc("vanished"))
    leads_for(session_factory, "Ghost Two", 1, description=ref_desc("vanished"))
    step(session_factory, leads_plan(sources_config, client), query)
    leads_for(session_factory, "Ghost Three", 1, description=ref_desc("vanished"))
    step(session_factory, leads_plan(sources_config, client), query)  # a later run
    gone = [u for u in world.requests if u.endswith("/boards/vanished")]
    assert len(gone) == 1  # same run: memo; later run: proven miss, reused


# --- 13-16: cross-company miss reuse -------------------------------------------------------------


def test_misses_are_reused_across_companies_until_they_expire(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    client: httpx.Client,
    query: SearchQuery,
    world: World,
) -> None:
    leads_for(session_factory, "Nowhere", 1)  # single guess 'nowhere' on 4 platforms
    step(session_factory, leads_plan(sources_config, client), query)
    first = len(world.requests)
    assert first == 4
    with session_factory() as db:
        misses = known_misses(db)
    assert ("greenhouse", "nowhere") in misses and len(misses) == 4
    # A different company whose guesses include 'nowhere' does not ask those 4 again.
    with session_factory() as db:
        # guesses: nowhereglobal, nowhere-global, nowhere
        add_adzuna_row(db, company="Nowhere Global", sid="ng")
    plan = leads_plan(sources_config, client, max_requests_per_company=12)
    outcome = step(session_factory, plan, query)
    assert outcome.metrics["miss_cache_hits"] == 4
    assert not any(u.rstrip("/").endswith("/nowhere") for u in world.requests[first:])
    with session_factory() as db:  # after 14 days the misses may be probed again
        for board in db.scalars(select(EmployerBoard)):
            board.next_check_at = utcnow() - timedelta(seconds=1)
        db.commit()
        assert known_misses(db) == set()


class FlakyWorld(World):
    """Greenhouse answers 500 for 'flaky' boards."""

    def __call__(self, r: httpx.Request) -> httpx.Response:
        if r.url.host == "boards-api.greenhouse.io" and "flaky" in r.url.path:
            self.requests.append(str(r.url))
            return httpx.Response(500)
        return super().__call__(r)


def test_errors_and_429s_are_never_recorded_as_misses(
    session_factory: sessionmaker[Session],
    sources_config: JobSourcesConfig,
    query: SearchQuery,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration.test_leads import SharedClient

    flaky = FlakyWorld()
    flaky.sr_status = 429
    client = SharedClient(transport=httpx.MockTransport(flaky))
    leads_for(session_factory, "Flaky", 1)
    step(session_factory, leads_plan(sources_config, client), query)
    with session_factory() as db:
        misses = known_misses(db)
        board = db.scalars(select(EmployerBoard)).one()
    assert ("greenhouse", "flaky") not in misses  # 500: an error, retried later
    assert not any(p == "smartrecruiters" for p, _ in misses)  # 429: never a miss
    assert {("ashby", "flaky"), ("lever", "flaky")} <= misses  # real 404s are
    assert board.evidence["stopped_platforms"] == ["smartrecruiters"]


# --- budgets unchanged ---------------------------------------------------------------------------


def test_budget_and_cap_defaults_are_unchanged() -> None:
    rules = LeadResolutionConfig()
    assert rules.max_requests_per_run == 60 and rules.max_requests_per_company == 8
    assert rules.negative_cache_days == 14
    assert rules.enabled is False and rules.auto_activate_verified is False
