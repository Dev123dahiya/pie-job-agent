"""Lead -> trusted ATS resolution (Phase 5.7).

    untrusted aggregator (Adzuna row / Himalayas) -> job_leads   (never a job, never fresh)
      -> employer resolution (cached, budgeted)   -> employer_boards (CANDIDATE / VERIFIED)
      -> approved, active boards only             -> the EXISTING adapter + DiscoveryService
      -> strict same-job matching                 -> lead linked to the trusted ATS job
      -> the existing analysis / shortlist steps  (only for the trusted job, once)

Timestamps: nothing here ever writes a posted_at. A trusted ATS job gets its date from its own
adapter inside DiscoveryService; an aggregator timestamp is only stored on the lead, marked
untrusted. Matching only links records, it never moves a timestamp.
"""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx2 as httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.core.config import Settings
from backend.core.normalize import normalize_company, seniority_evidence, title_matches
from backend.services.dedupe import SameJobSubject, explicit_ats_boards, match_same_job
from backend.services.discovery import (
    DiscoveryService,
    _excluded,
    create_discovery_run,
    location_matches,
    mark_duplicate_of,
)
from backend.services.runs import RunLogger
from database.base import utcnow
from database.enums import BoardStatus, JobLogAction, LeadStatus
from database.models import Company, EmployerBoard, Job, JobLead
from integrations.job_sources.base import LeadRecord, SearchQuery, parse_datetime
from integrations.job_sources.board_probe import (
    PLATFORMS,
    AtsProbe,
    BoardProbe,
    CompanyBudgetExhausted,
    ProbeBudgetExhausted,
    mention_ratio,
)
from integrations.job_sources.common import company_entries
from integrations.job_sources.config import (
    BoardOverride,
    JobSourcesConfig,
    LeadResolutionConfig,
)
from integrations.job_sources.himalayas import HimalayasLeadSource, strip_query
from integrations.job_sources.registry import BOARD_KEYS, build_source_for_boards

logger = logging.getLogger(__name__)

MAX_DESCRIPTION = 4000
# Generic words dropped for one extra board-id guess ("Acme Technologies" -> "acme").
_GENERIC = {
    "technologies", "technology", "tech", "labs", "solutions", "software", "systems", "india",
    "group", "global", "services", "consulting", "digital", "ai", "io",
}  # fmt: skip
MATCHABLE = (
    LeadStatus.VERIFIED, LeadStatus.BOARD_CANDIDATE, LeadStatus.NO_MATCH, LeadStatus.AMBIGUOUS,
)  # fmt: skip
LEAD_METRIC_KEYS = [
    "leads_discovered", "leads_registered", "leads_already_known", "leads_filtered",
    "leads_rejected", "board_candidates", "boards_verified", "boards_activated",
    "leads_resolved", "leads_matched", "leads_duplicate", "leads_ambiguous", "leads_no_match",
    "leads_no_board", "trusted_jobs_created", "existing_jobs_linked", "boards_crawled",
    "requests_used", "rate_limit_stops", "negative_cache_hits", "budget_exhausted",
    "companies_pending", "companies_resolved_free", "companies_probed", "companies_deferred",
    "companies_no_board", "probe_cache_hits", "miss_cache_hits", "explicit_references",
    "companies_entry_evidence", "companies_senior_only",
]  # fmt: skip


@dataclass
class LeadsPlan:
    rules: LeadResolutionConfig
    sources_config: JobSourcesConfig
    settings: Settings
    # Test seams: an injected HTTP transport client and a no-op sleep. Never used for creds.
    client: httpx.Client | None = None
    sleep: Any = None
    # The candidate's VERIFIED full-time experience (profile). Used only to order the queue:
    # a lead asking for more than this is evidence the employer's posting does not fit.
    candidate_years: float | None = None


@dataclass
class LeadsOutcome:
    metrics: dict[str, int] = field(default_factory=lambda: dict.fromkeys(LEAD_METRIC_KEYS, 0))
    crawl_run_ids: list[uuid.UUID] = field(default_factory=list)
    linked_job_ids: set[uuid.UUID] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


# --- registration ------------------------------------------------------------------------


def adzuna_lead_records(db: Session, since: datetime) -> list[LeadRecord]:
    """Adzuna rows seen since `since` (they keep posted_at = NULL: `created` is untrusted)."""
    rows = db.scalars(
        select(Job).where(
            Job.source == "adzuna",
            Job.last_seen_at >= since,
            Job.duplicate_of_id.is_(None),
        )
    ).all()
    return [
        LeadRecord(
            source="adzuna",
            source_lead_id=job.source_job_id,
            company=job.company.name if job.company else None,
            title=job.title,
            location=job.location,
            description=job.description,
            url=job.url,
            aggregator_timestamp=parse_datetime((job.raw or {}).get("adzuna_created")),
            aggregator_job_id=str(job.id),
        )
        for job in rows
    ]


def register_leads(
    db: Session, records: list[LeadRecord], query: SearchQuery, metrics: dict[str, int]
) -> list[JobLead]:
    """Store/refresh leads. Only postings that match the configured roles and locations are
    kept; excluded companies are stored as REJECTED and never resolved."""
    now = utcnow()
    touched: list[JobLead] = []
    for record in records:
        metrics["leads_discovered"] += 1
        if not title_matches(record.title, query.terms, query.term_aliases) or (
            location_matches(record.location, query) is None
        ):
            metrics["leads_filtered"] += 1
            continue
        lead = db.scalar(
            select(JobLead).where(
                JobLead.source == record.source, JobLead.source_lead_id == record.source_lead_id
            )
        )
        if lead is not None:
            lead.last_seen_at = now
            lead.times_seen += 1
            metrics["leads_already_known"] += 1
            touched.append(lead)
            continue
        excluded = _excluded(record.company, query.excluded_companies)
        lead = JobLead(
            source=record.source,
            source_lead_id=record.source_lead_id[:512],
            company_name=record.company,
            normalized_company=normalize_company(record.company) if record.company else None,
            company_hint=record.company_hint,
            title=record.title[:512],
            location=record.location,
            description=(record.description or "")[:MAX_DESCRIPTION] or None,
            source_url=strip_query(record.url),
            first_seen_at=now,
            last_seen_at=now,
            aggregator_timestamp=record.aggregator_timestamp,
            aggregator_timestamp_trusted=False,
            status=LeadStatus.REJECTED if excluded else LeadStatus.NEW,
            aggregator_job_id=uuid.UUID(record.aggregator_job_id)
            if record.aggregator_job_id
            else None,
            evidence={"rejected": "excluded company"} if excluded else None,
        )
        db.add(lead)
        metrics["leads_rejected" if excluded else "leads_registered"] += 1
        touched.append(lead)
    db.flush()
    return touched


# --- employer resolution -----------------------------------------------------------------


def _compact(normalized: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalized)


def board_id_guesses(normalized: str, hint: str | None) -> list[str]:
    """At most 3 board-id guesses: the aggregator's slug, the compact name, the name without
    generic words. Nothing shorter than 2 characters."""
    tokens = normalized.split()
    core = [t for t in tokens if t not in _GENERIC]
    guesses = [
        (hint or "").lower().strip(),
        _compact(normalized),
        "-".join(tokens),
        _compact(" ".join(core)) if core and core != tokens else "",
    ]
    return [g for g in dict.fromkeys(guesses) if len(g) >= 2][:3]


def configured_boards(config: JobSourcesConfig) -> dict[str, tuple[str, str]]:
    """normalized company -> (platform, board id) for the boards in job_sources.yaml."""
    out: dict[str, tuple[str, str]] = {}
    for source in config.sources:
        key = BOARD_KEYS.get(source.type)
        if key is None:
            continue
        for raw in source.options.get(key) or []:
            if isinstance(raw, str):  # a bare Greenhouse token: its own compact name
                out.setdefault(raw.lower(), (source.type, raw))
            for board, name in company_entries([raw]) if isinstance(raw, dict) else []:
                out.setdefault(normalize_company(name), (source.type, board))
    return out


def _configured_for(normalized: str, index: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    return index.get(normalized) or index.get(_compact(normalized))


def verify(
    probe: BoardProbe, normalized: str, company: str, rules: LeadResolutionConfig
) -> tuple[BoardStatus, dict[str, Any]]:
    """CANDIDATE unless identity is proven. Boards with ids of <= 3 characters are never
    verified automatically."""
    evidence: dict[str, Any] = {"platform": probe.platform, "board_id": probe.board_id}
    if len(probe.board_id) <= 3:
        return BoardStatus.CANDIDATE, {**evidence, "rule": "short_board_id"}
    if probe.platform in ("greenhouse", "smartrecruiters"):
        same = bool(probe.name) and normalize_company(probe.name or "") == normalized
        return (BoardStatus.VERIFIED if same else BoardStatus.CANDIDATE), {
            **evidence, "rule": "board_name", "board_name": probe.name,
        }  # fmt: skip
    ratio = mention_ratio(probe.texts, company)
    ok = (
        probe.postings >= rules.min_postings_for_mention
        and ratio >= rules.description_mention_threshold
    )
    return (BoardStatus.VERIFIED if ok else BoardStatus.CANDIDATE), {
        **evidence, "rule": "description_mentions", "ratio": round(ratio, 3),
        "postings": probe.postings, "threshold": rules.description_mention_threshold,
    }  # fmt: skip


def _upsert_board(
    db: Session,
    normalized: str,
    company: str,
    platform: str | None,
    board_id: str | None,
    status: BoardStatus,
    evidence: dict[str, Any],
    *,
    configured: bool = False,
    next_check_at: datetime | None = None,
) -> EmployerBoard:
    now = utcnow()
    if platform is None:
        board = db.scalar(
            select(EmployerBoard).where(
                EmployerBoard.normalized_company == normalized,
                EmployerBoard.status == BoardStatus.NOT_FOUND,
            )
        )
    else:
        board = db.scalar(
            select(EmployerBoard).where(
                EmployerBoard.platform == platform,
                func.lower(EmployerBoard.board_id) == (board_id or "").lower(),
            )
        )
    if board is None:
        board = EmployerBoard(
            normalized_company=normalized,
            company_name=company,
            platform=platform,
            board_id=board_id,
            status=status,
            active=False,
            configured=configured,
        )
        db.add(board)
    elif board.status in (BoardStatus.REJECTED,) or (
        board.status is BoardStatus.VERIFIED and status is not BoardStatus.VERIFIED
    ):
        board.last_checked_at = now
        return board  # never downgrade a verified board or undo the user's rejection
    else:
        board.status = status
        board.configured = board.configured or configured
    board.evidence = evidence
    board.last_checked_at = now
    board.next_check_at = next_check_at
    db.flush()
    return board


def trusted_board_index(db: Session) -> dict[str, tuple[str, str]]:
    """normalized company -> (platform, board id) proven by trusted ATS jobs PIE already
    stores (they only come from configured or approved boards). A company seen on more than
    one board keeps the board with the most postings."""
    rows = db.execute(
        select(Company.name, Job.source, Job.source_job_id)
        .join(Company, Company.id == Job.company_id)
        .where(Job.source.in_(PLATFORMS), Job.duplicate_of_id.is_(None))
    ).all()
    counts: dict[str, dict[tuple[str, str], int]] = {}
    for name, platform, source_job_id in rows:
        board = str(source_job_id).split(":", 1)[0]
        per = counts.setdefault(normalize_company(name), {})
        per[(platform, board)] = per.get((platform, board), 0) + 1
    return {
        company: sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        for company, per in counts.items()
    }


def _trusted_hit(
    normalized: str,
    configured: dict[str, tuple[str, str]],
    trusted: dict[str, tuple[str, str]],
) -> tuple[str, str] | None:
    """A trusted-jobs mapping counts only for boards that are in job_sources.yaml."""
    hit = trusted.get(normalized)
    configured_boards_ = {(p, b.lower()) for p, b in configured.values()}
    return hit if hit is not None and (hit[0], hit[1].lower()) in configured_boards_ else None


def cached_board(
    db: Session,
    normalized: str,
    company: str,
    rules: LeadResolutionConfig,
    configured: dict[str, tuple[str, str]],
    trusted: dict[str, tuple[str, str]],
    metrics: dict[str, int],
) -> EmployerBoard | None:
    """A resolution that needs NO request, or None. In order: a verified board, a board in
    job_sources.yaml, a configured board proven by stored trusted ATS jobs, the negative
    cache, a recently checked candidate."""
    now = utcnow()
    known = db.scalars(
        select(EmployerBoard).where(EmployerBoard.normalized_company == normalized)
    ).all()
    verified = [b for b in known if b.status is BoardStatus.VERIFIED]
    if verified:
        return sorted(verified, key=lambda b: (not b.configured, not b.active))[0]
    hit = _configured_for(normalized, configured)
    rule = "configured_board"
    if hit is None:
        hit, rule = _trusted_hit(normalized, configured, trusted), "trusted_ats_jobs"
    if hit is not None:
        return _upsert_board(
            db, normalized, company, hit[0], hit[1], BoardStatus.VERIFIED,
            {"rule": rule}, configured=True,
        )  # fmt: skip
    negative = next((b for b in known if b.status is BoardStatus.NOT_FOUND), None)
    if negative and negative.next_check_at and negative.next_check_at > now:
        metrics["negative_cache_hits"] += 1
        return negative
    candidates = [b for b in known if b.status is BoardStatus.CANDIDATE]
    recent = now - timedelta(days=rules.negative_cache_days)
    if candidates and all(b.last_checked_at and b.last_checked_at > recent for b in candidates):
        return candidates[0]
    return None


def _override_for(normalized: str, rules: LeadResolutionConfig) -> BoardOverride | None:
    return next(
        (o for name, o in rules.overrides.items() if normalize_company(name) == normalized), None
    )


def lead_references(leads: list[JobLead]) -> list[tuple[str, str]]:
    """Supported-ATS boards the leads themselves quote (a posting URL in the description or
    link), most-quoted first, then alphabetical."""
    counts: dict[tuple[str, str], int] = {}
    for lead in leads:
        for ref in explicit_ats_boards(lead.source_url, lead.description):
            counts[ref] = counts.get(ref, 0) + 1
    return [ref for ref, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def known_misses(db: Session) -> set[tuple[str, str]]:
    """(platform, board id) pairs the ATS APIs answered 'no such board' for, recorded on
    negative-cache entries that have not expired. Errors, timeouts and 429s are never
    recorded as misses (see AtsProbe), so they are always retried later."""
    now = utcnow()
    rows = db.scalars(
        select(EmployerBoard).where(
            EmployerBoard.status == BoardStatus.NOT_FOUND, EmployerBoard.next_check_at > now
        )
    ).all()
    out: set[tuple[str, str]] = set()
    for board in rows:
        for pair in (board.evidence or {}).get("misses") or []:
            if isinstance(pair, str) and ":" in pair:
                platform, _, board_id = pair.partition(":")
                out.add((platform, board_id.lower()))
    return out


def _owned_by_other_company(db: Session, platform: str, board_id: str, normalized: str) -> bool:
    board = db.scalar(
        select(EmployerBoard).where(
            EmployerBoard.platform == platform,
            func.lower(EmployerBoard.board_id) == board_id.lower(),
        )
    )
    return board is not None and board.normalized_company != normalized


def probe_company(
    db: Session,
    normalized: str,
    company: str,
    hint: str | None,
    probe: AtsProbe,
    rules: LeadResolutionConfig,
    references: list[tuple[str, str]] | None = None,
) -> EmployerBoard:
    """Look for the employer's board with requests, within max_requests_per_company:
    the override, then the ONE board the leads quote (if any), then the id guesses across all
    platforms (guess-major). Every board found goes through the unchanged identity rules; a
    board already recorded for a different company is never reused. Raises
    ProbeBudgetExhausted if the RUN budget ends first: the company is then left for the next
    run, never negative-cached."""
    now = utcnow()
    probe.start_company(rules.max_requests_per_company)
    tried: list[str] = []
    misses: list[str] = []
    fallback: EmployerBoard | None = None
    complete = True

    def check(platform: str, board_id: str, rule: str | None = None) -> EmployerBoard | None:
        nonlocal fallback
        found = probe.probe(platform, board_id)
        tried.append(f"{platform}:{board_id}")
        if found is None:
            if probe.is_miss(platform, board_id):
                misses.append(f"{platform}:{board_id.lower()}")
            return None
        if _owned_by_other_company(db, found.platform, found.board_id, normalized):
            return None  # another company's board: never evidence for this one
        if rule == "config_override":
            status, evidence = BoardStatus.VERIFIED, {"rule": rule, "board_name": found.name}
        else:
            status, evidence = verify(found, normalized, company, rules)
            if rule:
                evidence = {**evidence, "found_via": rule}
        board = _upsert_board(
            db, normalized, company, found.platform, found.board_id, status, evidence
        )
        if board.status is BoardStatus.VERIFIED:
            return board
        fallback = fallback or board
        return None

    try:
        override = _override_for(normalized, rules)
        if override is not None:
            board = check(override.platform, override.board, "config_override")
            if board is not None:
                return board
        if references:  # exactly one request for the board the leads themselves name
            platform, board_id = references[0]
            board = check(platform, board_id, "explicit_ats_reference")
            if board is not None:
                return board
        settled = {fallback.platform} if fallback is not None and fallback.platform else set()
        for guess in board_id_guesses(normalized, hint):
            for platform in PLATFORMS:
                if platform in settled or platform in probe.stopped:
                    continue
                if f"{platform}:{guess}" in tried:
                    continue
                board = check(platform, guess)
                if board is not None:
                    return board
                if fallback is not None and fallback.platform:
                    settled.add(fallback.platform)
    except CompanyBudgetExhausted:
        complete = False
    finally:
        probe.start_company(rules.max_requests_per_run)  # lift the per-company cap
    if fallback is not None:
        return fallback
    return _upsert_board(
        db, normalized, company, None, None, BoardStatus.NOT_FOUND,
        {"rule": "no_board", "tried": tried, "misses": sorted(set(misses)),
         "complete": complete, "stopped_platforms": sorted(probe.stopped)},
        next_check_at=now + timedelta(days=rules.negative_cache_days),
    )  # fmt: skip


TIER_FREE, TIER_OVERRIDE, TIER_REFERENCE, TIER_NEW, TIER_RECHECK = 0, 1, 2, 3, 4
LEAD_BUCKET_CAP = 3  # 1, 2, 3+ leads
# Candidate fit of a company's leads (lower first): at least one lead with entry-level
# evidence; no evidence either way; every lead shows senior evidence.
FIT_ENTRY, FIT_UNKNOWN, FIT_SENIOR_ONLY = 0, 1, 2


def company_fit(leads: list[JobLead], candidate_years: float | None) -> int:
    """How well the employer's OWN lead postings fit the candidate, from their text only
    (see normalize.seniority_evidence). Orders the queue; never decides eligibility."""
    kinds = {seniority_evidence(lead.title, lead.description, candidate_years) for lead in leads}
    if "entry" in kinds:
        return FIT_ENTRY
    return FIT_SENIOR_ONLY if kinds == {"senior"} else FIT_UNKNOWN


@dataclass
class QueueItem:
    """One company to resolve; the queue is sorted by `key` (lower first)."""

    normalized: str
    leads: list[JobLead]
    tier: int
    cost: int  # requests the company needs at most (0 = free)
    references: list[tuple[str, str]]
    key: tuple[Any, ...]
    fit: int = FIT_UNKNOWN


def resolution_cost(
    normalized: str,
    hint: str | None,
    rules: LeadResolutionConfig,
    misses: set[tuple[str, str]],
) -> int:
    """Requests guessing will need at most: platform x id-guess pairs not already proven to
    be misses, capped at max_requests_per_company."""
    pairs = [
        (platform, guess)
        for guess in board_id_guesses(normalized, hint)
        for platform in PLATFORMS
        if (platform, guess.lower()) not in misses
    ]
    return min(len(pairs), rules.max_requests_per_company)


def resolution_queue(
    db: Session,
    leads: list[JobLead],
    rules: LeadResolutionConfig,
    configured: dict[str, tuple[str, str]],
    trusted: dict[str, tuple[str, str]],
    misses: set[tuple[str, str]] | None = None,
    candidate_years: float | None = None,
) -> list[QueueItem]:
    """The deterministic order in which companies are resolved - only the order: nothing
    here decides eligibility, freshness or a match. Key (lower first):

      1. tier     0 free: a verified board, a configured board (by name or proven by stored
                    trusted jobs), an unexpired negative cache or a recent candidate
                  1 an explicit override (1 request)
                  2 a supported-ATS board quoted in a lead (1 request for that board)
                  3 never checked
                  4 checked before, cache expired
      2. fit      candidate fit of the company's own leads: entry-level evidence, then no
                  evidence, then senior-only (every lead senior-titled or asking for more
                  experience than the candidate's verified years) - a board is only worth
                  finding if its postings can fit; senior-only employers go last, not never
      3. cost     requests needed at most (0 for free, 1 for an override or a quoted board,
                  else untried platform x guess pairs, capped at max_requests_per_company):
                  cheaper first, so the budget checks the most companies
      4. -bucket  pending leads, capped at 3 (1, 2, 3+): a tie-breaker between equal costs
                  only - 3 and 30 leads rank the same, and lead count never beats cost
      5. -seen    most recently seen lead first (PIE's own clock)
      6. company  normalized name
    Aggregator dates are deliberately not part of the key.
    """
    misses = misses or set()
    now = utcnow()
    by_company: dict[str, list[JobLead]] = {}
    for lead in leads:
        by_company.setdefault(lead.normalized_company or "", []).append(lead)
    known: dict[str, list[EmployerBoard]] = {}
    for board in db.scalars(
        select(EmployerBoard).where(EmployerBoard.normalized_company.in_(list(by_company)))
    ).all():
        known.setdefault(board.normalized_company, []).append(board)
    recent = now - timedelta(days=rules.negative_cache_days)
    items = []
    for company, group in by_company.items():
        rows = known.get(company, [])
        free = (
            any(b.status is BoardStatus.VERIFIED for b in rows)
            or _configured_for(company, configured) is not None
            or _trusted_hit(company, configured, trusted) is not None
            or any(
                b.status is BoardStatus.NOT_FOUND
                and b.next_check_at is not None
                and b.next_check_at > now
                for b in rows
            )
            or any(
                b.status is BoardStatus.CANDIDATE
                and b.last_checked_at is not None
                and b.last_checked_at > recent
                for b in rows
            )
        )
        references = lead_references(group)
        hint = next((lead.company_hint for lead in group if lead.company_hint), None)
        if free:
            tier, cost = TIER_FREE, 0
        elif _override_for(company, rules) is not None:
            tier, cost = TIER_OVERRIDE, 1
        elif references:
            tier, cost = TIER_REFERENCE, 1
        else:
            tier = TIER_RECHECK if rows else TIER_NEW
            cost = resolution_cost(company, hint, rules, misses)
        seen = max(lead.last_seen_at for lead in group)
        fit = company_fit(group, candidate_years)
        key = (tier, fit, cost, -min(len(group), LEAD_BUCKET_CAP), -seen.timestamp(), company)
        items.append(QueueItem(company, group, tier, cost, references, key, fit))
    return sorted(items, key=lambda item: item.key)


def resolve_leads(db: Session, plan: LeadsPlan, log: RunLogger, outcome: LeadsOutcome) -> None:
    """Resolve pending leads company by company, in resolution_queue order. Each company is
    resolved once per run and all its leads share the result. Free resolutions always run and
    cost nothing; companies that need requests are limited by max_companies_per_run,
    max_requests_per_company and max_requests_per_run."""
    rules, metrics = plan.rules, outcome.metrics
    pending = db.scalars(
        select(JobLead).where(
            JobLead.status == LeadStatus.NEW, JobLead.normalized_company.is_not(None)
        )
    ).all()
    configured = configured_boards(plan.sources_config)
    trusted = trusted_board_index(db)
    misses = known_misses(db)
    queue = resolution_queue(
        db, list(pending), rules, configured, trusted, misses, plan.candidate_years
    )
    metrics["companies_pending"] += len(queue)
    metrics["companies_entry_evidence"] += sum(1 for i in queue if i.fit == FIT_ENTRY)
    metrics["companies_senior_only"] += sum(1 for i in queue if i.fit == FIT_SENIOR_ONLY)
    probe = AtsProbe(
        user_agent=plan.settings.http_user_agent,
        max_requests=rules.max_requests_per_run,
        delay_seconds=rules.request_delay_seconds,
        client=plan.client,
        **({"sleep": plan.sleep} if plan.sleep else {}),
    )
    probe.preload_misses(misses)
    probed = 0
    try:
        for item in queue:
            first = item.leads[0]
            company = first.company_name or item.normalized
            board = cached_board(db, item.normalized, company, rules, configured, trusted, metrics)
            if board is not None:
                metrics["companies_resolved_free"] += 1
            else:
                if probed >= rules.max_companies_per_run or metrics["budget_exhausted"]:
                    metrics["companies_deferred"] += 1
                    continue
                hint = next((lead.company_hint for lead in item.leads if lead.company_hint), None)
                if item.references:
                    metrics["explicit_references"] += 1
                try:
                    board = probe_company(
                        db, item.normalized, company, hint, probe, rules, item.references
                    )
                except ProbeBudgetExhausted:
                    metrics["budget_exhausted"] = 1
                    metrics["companies_deferred"] += 1
                    db.rollback()
                    continue
                probed += 1
                metrics["companies_probed"] += 1
            if (
                board.status is BoardStatus.VERIFIED
                and rules.auto_activate_verified
                and not board.active
                and not board.configured
            ):
                board.active = True
                board.approved_at = utcnow()
                metrics["boards_activated"] += 1
            lead_status = {
                BoardStatus.VERIFIED: LeadStatus.VERIFIED,
                BoardStatus.CANDIDATE: LeadStatus.BOARD_CANDIDATE,
            }.get(board.status, LeadStatus.NO_BOARD)
            metrics[
                {
                    LeadStatus.VERIFIED: "boards_verified",
                    LeadStatus.BOARD_CANDIDATE: "board_candidates",
                }.get(lead_status, "companies_no_board")
            ] += 1
            for lead in item.leads:
                lead.status = lead_status
                lead.employer_board_id = board.id if board.platform else None
                lead.attempts += 1
                lead.resolved_at = utcnow()
                metrics["leads_resolved"] += 1
                if lead_status is LeadStatus.NO_BOARD:
                    metrics["leads_no_board"] += 1
            log.log(
                JobLogAction.BOARD_RESOLVED,
                input={
                    "company": item.normalized,
                    "leads": len(item.leads),
                    "tier": item.tier,
                    "fit": item.fit,
                    "cost": item.cost,
                },
                output={
                    "board": board.label,
                    "status": board.status.value,
                    "evidence": board.evidence or {},
                },
            )
            db.commit()
    finally:
        metrics["requests_used"] += probe.requests_used
        metrics["probe_cache_hits"] += probe.cache_hits
        metrics["miss_cache_hits"] += probe.miss_cache_hits
        metrics["rate_limit_stops"] += len(probe.stopped)
        outcome.errors.extend(probe.errors)
        probe.close()


# --- crawling approved boards ------------------------------------------------------------


def crawl_active_boards(
    factory: sessionmaker[Session],
    plan: LeadsPlan,
    query: SearchQuery,
    dev_mode: bool,
    outcome: LeadsOutcome,
) -> None:
    """Crawl VERIFIED + active boards that are not already in job_sources.yaml, with the
    platform's existing adapter and the normal DiscoveryService (all filters apply)."""
    with factory() as db:
        boards = db.scalars(
            select(EmployerBoard)
            .where(
                EmployerBoard.status == BoardStatus.VERIFIED,
                EmployerBoard.active.is_(True),
                EmployerBoard.configured.is_(False),
                EmployerBoard.platform.is_not(None),
            )
            .order_by(EmployerBoard.approved_at)
            .limit(plan.rules.max_boards_crawled_per_run)
        ).all()
        by_platform: dict[str, list[tuple[str, str]]] = {}
        for board in boards:
            by_platform.setdefault(board.platform or "", []).append(
                (board.board_id or "", board.company_name)
            )
    for platform, entries in by_platform.items():
        try:
            source = build_source_for_boards(platform, entries, plan.sources_config, plan.settings)
        except ValueError as exc:
            outcome.errors.append(str(exc))
            continue
        with factory() as db:
            run = create_discovery_run(db, source, query, dev_mode, check_active=False)
            run.task = f"Discover jobs from lead boards on {platform}"
            db.commit()
        summary = DiscoveryService(factory, plan.rules).execute(run.id, source, query)
        outcome.crawl_run_ids.append(summary.run_id)
        outcome.metrics["boards_crawled"] += len(entries)
        outcome.metrics["trusted_jobs_created"] += summary.jobs_new


# --- strict same-job matching ------------------------------------------------------------


def match_leads(
    db: Session, plan: LeadsPlan, query: SearchQuery, log: RunLogger, outcome: LeadsOutcome
) -> None:
    rules, metrics = plan.rules, outcome.metrics
    since = utcnow() - timedelta(days=rules.lead_match_days)
    leads = db.scalars(
        select(JobLead)
        .join(EmployerBoard, EmployerBoard.id == JobLead.employer_board_id)
        .where(
            JobLead.status.in_(MATCHABLE),
            JobLead.last_seen_at >= since,
            EmployerBoard.status == BoardStatus.VERIFIED,
            (EmployerBoard.active.is_(True)) | (EmployerBoard.configured.is_(True)),
        )
    ).all()
    crawl_runs = set(outcome.crawl_run_ids)
    for lead in leads:
        board = lead.board
        assert board is not None and board.platform and board.board_id
        prefix = f"{board.board_id.lower()}:"
        candidates = db.scalars(
            select(Job).where(
                Job.source == board.platform,
                func.lower(func.substr(Job.source_job_id, 1, len(prefix))) == prefix,
                Job.duplicate_of_id.is_(None),
            )
        ).all()
        result = match_same_job(
            SameJobSubject(
                title=lead.title,
                location=lead.location,
                description=lead.description,
                first_seen_at=lead.first_seen_at,
                url=lead.source_url,
            ),
            list(candidates),
            lambda loc: location_matches(loc, query),
            tolerance=timedelta(hours=rules.match_timing_tolerance_hours),
            min_description_chars=rules.min_description_chars,
            min_overlap=rules.min_description_overlap,
        )
        lead.attempts += 1
        lead.evidence = {**result.evidence, "candidates": len(candidates)}
        if result.status == "MATCH" and result.job is not None:
            job = result.job
            new_via_lead = job.discovery_run_id in crawl_runs
            lead.status = LeadStatus.MATCHED if new_via_lead else LeadStatus.DUPLICATE
            lead.matched_job_id = job.id
            lead.resolved_at = utcnow()
            metrics["leads_matched" if new_via_lead else "leads_duplicate"] += 1
            if not new_via_lead:
                metrics["existing_jobs_linked"] += 1
            outcome.linked_job_ids.add(job.id)
            if lead.aggregator_job_id:
                aggregator = db.get(Job, lead.aggregator_job_id)
                if aggregator is not None and aggregator.duplicate_of_id is None:
                    mark_duplicate_of(db, aggregator, job, result.evidence)
            log.log(
                JobLogAction.LEAD_MATCHED,
                job_id=job.id,
                input={"lead_id": str(lead.id), "source": lead.source},
                output={"status": lead.status.value, **result.evidence},
            )
        else:
            lead.status = (
                LeadStatus.AMBIGUOUS if result.status == "AMBIGUOUS" else LeadStatus.NO_MATCH
            )
            metrics["leads_ambiguous" if result.status == "AMBIGUOUS" else "leads_no_match"] += 1
    db.commit()


# --- the whole step ------------------------------------------------------------------------


def run_leads_step(
    factory: sessionmaker[Session],
    run_log_db: Session,
    log: RunLogger,
    plan: LeadsPlan,
    query: SearchQuery,
    since: datetime,
    dev_mode: bool,
) -> LeadsOutcome:
    """register (Adzuna rows seen this run + Himalayas) -> resolve -> crawl -> match."""
    outcome = LeadsOutcome()
    db = run_log_db
    records = adzuna_lead_records(db, since)
    if plan.rules.himalayas.enabled:
        himalayas = HimalayasLeadSource(
            plan.rules.himalayas,
            query.terms,
            user_agent=plan.settings.http_user_agent,
            client=plan.client,
            **({"sleep": plan.sleep} if plan.sleep else {}),
        )
        try:
            records += himalayas.collect()
        finally:
            outcome.metrics["requests_used"] += himalayas.http.requests
            outcome.metrics["rate_limit_stops"] += int(himalayas.rate_limited)
            outcome.errors.extend(himalayas.errors)
            himalayas.close()
    register_leads(db, records, query, outcome.metrics)
    log.log(
        JobLogAction.LEADS_REGISTERED,
        output={k: outcome.metrics[k] for k in LEAD_METRIC_KEYS[:5]},
    )
    db.commit()
    resolve_leads(db, plan, log, outcome)
    crawl_active_boards(factory, plan, query, dev_mode, outcome)
    db.expire_all()
    match_leads(db, plan, query, log, outcome)
    return outcome
