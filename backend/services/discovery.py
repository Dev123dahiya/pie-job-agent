"""Discovery pipeline: SOURCE -> DISCOVER -> NORMALIZE -> FILTER -> DEDUPLICATE -> STORE.

Failures are isolated: a bad posting is counted and skipped, a failing search task is
logged and the run moves on. Every step is written to the run's action log.
"""

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from backend.core.normalize import normalize_company, title_matches
from backend.services.dedupe import (
    AGGREGATOR_SOURCES,
    DedupeKeys,
    DuplicateMatch,
    SameJobSubject,
    find_duplicate,
    match_same_job,
)
from backend.services.freshness import compute_freshness
from backend.services.runs import RunLogger, cancel_requested, ensure_no_active_run, finish_run
from database.base import utcnow
from database.enums import (
    AgentActionStatus,
    AgentRunStatus,
    EventSource,
    JobLogAction,
    JobPipelineState,
    RunType,
)
from database.models import AgentRun, ApplicationEvent, Job
from database.repositories import CompanyRepository
from integrations.job_sources.base import (
    JobCandidate,
    JobSource,
    RawJob,
    SearchQuery,
    SourceAuthRequired,
    SourceError,
    SourceRateLimited,
    SourceTimeout,
)
from integrations.job_sources.config import LeadResolutionConfig

logger = logging.getLogger(__name__)


@dataclass
class DiscoverySummary:
    run_id: uuid.UUID
    status: AgentRunStatus
    searches_total: int
    searches_completed: int
    jobs_found: int
    jobs_new: int
    jobs_duplicate: int
    jobs_failed: int
    errors: int


# Per-search counters (the same for every source), plus search-failure kinds per run.
STAT_KEYS = [
    "pages", "scanned", "failed", "rejected_role", "rejected_location", "matched",
    "excluded_company", "freshness_fresh", "freshness_stale", "freshness_unknown",
    "duplicate", "new",
]  # fmt: skip
FAILURE_KEYS = ["failed_auth", "failed_rate_limited", "failed_timeout", "failed_api"]


def failure_kind(exc: BaseException) -> str:
    if isinstance(exc, SourceAuthRequired):
        return "failed_auth"
    if isinstance(exc, SourceRateLimited):
        return "failed_rate_limited"
    if isinstance(exc, SourceTimeout):
        return "failed_timeout"
    return "failed_api"


def _excluded(company: str | None, excluded: list[str]) -> bool:
    """The same matching the shortlist uses (backend.services.shortlist.exclusion_check)."""
    if not company or not excluded:
        return False
    norm = normalize_company(company)
    return any(normalize_company(e) == norm for e in excluded)


def location_matches(location: str | None, query: SearchQuery) -> str | None:
    """Name of the first configured location the posting's location text matches."""
    if not location:
        return None
    for name in query.locations:
        patterns = query.location_patterns.get(name) or [re.escape(name)]
        if any(re.search(p, location, re.I) for p in patterns):
            return name
    return None


def candidate_matches(candidate: JobCandidate, query: SearchQuery) -> bool:
    return match_rejection(candidate, query) is None


def match_rejection(candidate: JobCandidate, query: SearchQuery) -> str | None:
    """'role' / 'location' if the posting is outside the configured search, else None."""
    if not title_matches(candidate.title, query.terms, query.term_aliases):
        return "role"
    if location_matches(candidate.location, query) is None:
        return "location"
    return None


def create_discovery_run(
    db: Session,
    source: JobSource,
    query: SearchQuery,
    dev_mode: bool,
    *,
    check_active: bool = True,
) -> AgentRun:
    """Register a run (fails if another run is active). Execution happens separately.

    check_active=False is only for steps of a parent run that already holds the lock."""
    if check_active:
        ensure_no_active_run(db)
    run = AgentRun(
        task=f"Discover jobs from {source.name}",
        run_type=RunType.DISCOVERY,
        status=AgentRunStatus.RUNNING,
        development_mode=dev_mode,
        source=source.name,
        search_terms=query.terms,
        locations=query.locations,
        current_task="Starting discovery",
        heartbeat_at=utcnow(),
    )
    db.add(run)
    db.commit()
    return run


class DiscoveryService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        lead_rules: LeadResolutionConfig | None = None,
    ) -> None:
        self.session_factory = session_factory
        # Only with lead resolution on: a trusted ATS posting that matches an aggregator row
        # (untrusted date) is stored as its own job instead of being folded into that row.
        self.lead_rules = lead_rules

    def execute(self, run_id: uuid.UUID, source: JobSource, query: SearchQuery) -> DiscoverySummary:
        with self.session_factory() as db:
            run = db.get(AgentRun, run_id)
            if run is None:
                raise ValueError(f"Run {run_id} not found")
            log = RunLogger(db, run)
            try:
                self._execute(db, run, log, source, query)
            except Exception as exc:  # the run must never be left RUNNING
                logger.exception("Discovery run %s crashed", run.id)
                db.rollback()
                run = db.get(AgentRun, run_id) or run
                log = RunLogger(db, run)
                log.error(f"Discovery crashed: {type(exc).__name__}: {exc}")
                log.log(
                    JobLogAction.DISCOVERY_COMPLETED,
                    status=AgentActionStatus.FAILED,
                    error=str(exc),
                )
                finish_run(run, AgentRunStatus.FAILED, summary="Discovery crashed; see errors.")
                db.commit()
            finally:
                source.close()
            return DiscoverySummary(
                run_id=run.id,
                status=run.status,
                searches_total=run.searches_total,
                searches_completed=run.searches_completed,
                jobs_found=run.jobs_found,
                jobs_new=run.jobs_new,
                jobs_duplicate=run.jobs_duplicate,
                jobs_failed=run.jobs_failed,
                errors=len(run.errors),
            )

    def _execute(
        self, db: Session, run: AgentRun, log: RunLogger, source: JobSource, query: SearchQuery
    ) -> None:
        tasks = source.plan(query)
        run.searches_total = len(tasks)
        log.log(
            JobLogAction.DISCOVERY_STARTED,
            input={"source": source.name, "terms": query.terms, "locations": query.locations},
            output={"searches": [t.label for t in tasks]},
        )
        log.heartbeat()
        db.commit()

        failed_tasks = 0
        totals: dict[str, int] = dict.fromkeys(STAT_KEYS + FAILURE_KEYS, 0)
        for task in tasks:
            if cancel_requested(db, run):
                log.log(JobLogAction.RUN_CANCELLED, output={"at_task": task.label})
                finish_run(
                    run, AgentRunStatus.STOPPED, summary=self._summary(run, "Stopped by user")
                )
                db.commit()
                return

            log.heartbeat(f"Searching {task.label}")
            db.commit()
            try:
                raws = source.search_jobs(task, query)
            except Exception as exc:  # one bad task never stops the run
                failed_tasks += 1
                kind = failure_kind(exc)
                totals[kind] += 1
                action = getattr(exc, "user_action", None)
                message = (
                    str(exc) if isinstance(exc, SourceError) else f"{type(exc).__name__}: {exc}"
                )
                log.error(message, task=task.label, **({"user_action": action} if action else {}))
                log.log(
                    JobLogAction.SEARCH_FAILED,
                    input={"task": task.label, "params": task.params},
                    output={"failure": kind},
                    status=AgentActionStatus.FAILED,
                    error=f"{message} | action: {action}" if action else message,
                )
                run.searches_completed += 1
                db.commit()
                continue

            stats: dict[str, int] = dict.fromkeys(STAT_KEYS, 0)
            stats["scanned"] = len(raws)
            stats["pages"] = int(getattr(source, "last_pages", 0) or 0)
            for raw in raws:
                self._process(db, run, log, source, query, raw, stats)
            for key, value in stats.items():
                totals[key] += value
            run.searches_completed += 1
            log.log(
                JobLogAction.SEARCH_COMPLETED,
                input={"task": task.label, "params": task.params},
                output=stats,
            )
            log.heartbeat()
            db.commit()

        all_failed = bool(tasks) and failed_tasks == len(tasks)
        status = AgentRunStatus.FAILED if all_failed else AgentRunStatus.COMPLETED
        log.log(
            JobLogAction.DISCOVERY_COMPLETED,
            status=AgentActionStatus.FAILED if all_failed else AgentActionStatus.SUCCESS,
            output={
                "found": run.jobs_found,
                "new": run.jobs_new,
                "duplicate": run.jobs_duplicate,
                "failed": run.jobs_failed,
                "totals": totals,
                "source": source.describe(),
            },
        )
        finish_run(
            run,
            status,
            summary=self._summary(run, "Completed" if not all_failed else "All searches failed"),
        )
        db.commit()

    def _process(
        self,
        db: Session,
        run: AgentRun,
        log: RunLogger,
        source: JobSource,
        query: SearchQuery,
        raw: RawJob,
        stats: dict[str, int],
    ) -> None:
        try:
            candidate = source.normalize_job(raw)
        except (ValidationError, ValueError, KeyError, TypeError) as exc:
            run.jobs_failed += 1
            stats["failed"] += 1
            log.log(
                JobLogAction.JOB_FAILED,
                input={"source_job_id": raw.source_job_id},
                status=AgentActionStatus.FAILED,
                error=f"Could not normalize: {exc}",
            )
            return

        rejection = match_rejection(candidate, query)
        if rejection is not None:
            stats[f"rejected_{rejection}"] += 1
            return  # outside the configured roles/locations; not counted as found

        run.jobs_found += 1
        stats["matched"] += 1
        # Diagnostics only - the exclusion and freshness rules still apply at analysis.
        if _excluded(candidate.company, query.excluded_companies):
            stats["excluded_company"] += 1
        if query.max_age_hours is not None:
            freshness = compute_freshness(candidate.posted_at, utcnow(), query.max_age_hours)
            stats[f"freshness_{freshness.value}"] += 1
        keys = DedupeKeys.for_candidate(candidate)
        log.log(
            JobLogAction.JOB_DISCOVERED,
            input={
                "source": candidate.source,
                "source_job_id": candidate.source_job_id,
                "company": candidate.company,
                "title": candidate.title,
                "location": candidate.location,
            },
        )

        duplicate = find_duplicate(db, candidate, keys)
        superseded: Job | None = None
        if duplicate is not None and self.lead_rules and _supersedes(duplicate, candidate):
            superseded, duplicate = duplicate.job, None
        if duplicate is not None:
            job = duplicate.job
            job.last_seen_at = utcnow()
            job.times_seen += 1
            _attribute(job, candidate)
            _learn_post_date(job, candidate, duplicate.reason, log)
            run.jobs_duplicate += 1
            stats["duplicate"] += 1
            log.log(
                JobLogAction.JOB_DUPLICATE,
                job_id=job.id,
                input={"source_job_id": candidate.source_job_id},
                output={"matched_on": duplicate.reason, "existing_job_id": str(job.id)},
            )
            return

        try:
            with db.begin_nested():
                try:
                    enriched = source.enrich(raw)
                    if enriched is not raw:
                        candidate = source.normalize_job(enriched)
                except (SourceError, ValidationError, ValueError) as exc:
                    # Detail fetch is optional; keep the list-level data.
                    log.log(
                        JobLogAction.JOB_NORMALIZED,
                        input={"source_job_id": raw.source_job_id},
                        status=AgentActionStatus.SKIPPED,
                        error=f"Detail fetch skipped: {exc}",
                    )
                job = self._store(db, run, candidate, keys)
        except Exception as exc:
            run.jobs_failed += 1
            stats["failed"] += 1
            log.log(
                JobLogAction.JOB_FAILED,
                input={"source_job_id": candidate.source_job_id},
                status=AgentActionStatus.FAILED,
                error=f"Could not store: {exc}",
            )
            return

        run.jobs_new += 1
        run.jobs_processed += 1
        stats["new"] += 1
        if superseded is not None and self.lead_rules is not None:
            link_aggregator_row(db, superseded, job, query, self.lead_rules, log)
        log.log(
            JobLogAction.JOB_NORMALIZED,
            job_id=job.id,
            output={
                "title": job.title,
                "posted_at": job.posted_at.isoformat() if job.posted_at else None,
                "salary_known": job.salary_min is not None or job.salary_max is not None,
                "experience_required": job.experience_required,
                "skills": job.skills,
            },
        )

    def _store(self, db: Session, run: AgentRun, c: JobCandidate, keys: DedupeKeys) -> Job:
        now = utcnow()
        company = CompanyRepository(db).get_or_create(c.company) if c.company else None
        job = Job(
            source=c.source,
            source_job_id=c.source_job_id,
            company=company,
            title=c.title,
            location=c.location,
            remote_type=c.remote_type,
            salary_min=c.salary_min,
            salary_max=c.salary_max,
            salary_currency=c.salary_currency,
            experience_required=c.experience_required,
            description=c.description,
            requirements=c.requirements,
            skills=c.skills,
            url=c.url,
            application_url=c.application_url,
            company_url=c.company_url,
            posted_at=c.posted_at,
            posted_at_kind=c.posted_at_kind,
            source_updated_at=c.source_updated_at,
            salary_period=c.salary_period,
            source_attributions=[_attribution(c, now)],
            discovered_at=now,
            last_seen_at=now,
            pipeline_state=JobPipelineState.DISCOVERED,
            raw=c.raw,
            canonical_url=keys.canonical_url,
            fingerprint=keys.fingerprint,
            discovery_run_id=run.id,
        )
        if c.salary_raw:  # left unset (SQL NULL) when the source disclosed nothing
            job.salary_raw = c.salary_raw
        db.add(job)
        db.flush()
        db.add(
            ApplicationEvent(
                job_id=job.id,
                occurred_at=now,
                event_type="job_discovered",
                source=EventSource.AGENT,
                new_status=JobPipelineState.DISCOVERED.value,
                reason=f"Discovered via {c.source}",
                evidence={"url": c.url, "run_id": str(run.id)},
            )
        )
        return job

    @staticmethod
    def _summary(run: AgentRun, headline: str) -> str:
        return (
            f"{headline}. Searches {run.searches_completed}/{run.searches_total}; "
            f"found {run.jobs_found}, new {run.jobs_new}, duplicates {run.jobs_duplicate}, "
            f"failed {run.jobs_failed}."
        )


def _attribution(c: JobCandidate, now: datetime) -> dict[str, Any]:
    return {
        "source": c.source,
        "source_job_id": c.source_job_id,
        "url": c.url,
        "posted_at": c.posted_at.isoformat() if c.posted_at else None,
        "posted_at_kind": c.posted_at_kind,
        "first_seen": now.isoformat(),
    }


def _attribute(job: Job, c: JobCandidate) -> None:
    """The same posting seen on another source (or under another id): keep the attribution.
    The stored job itself is not changed."""
    existing = list(job.source_attributions or [])
    if any(
        a.get("source") == c.source and a.get("source_job_id") == c.source_job_id for a in existing
    ):
        return
    job.source_attributions = [*existing, _attribution(c, utcnow())]


def _learn_post_date(job: Job, c: JobCandidate, reason: str, log: RunLogger) -> None:
    """A job stored without a post date may take one its OWN source publishes later.

    Only the same posting on the same source (source id or URL match) counts, and only a
    publication-type timestamp the adapter trusted (adapters leave posted_at None otherwise).
    An existing post date is never replaced, so a stale job can never turn fresh by being
    rediscovered, and nothing here uses discovery time."""
    if job.posted_at is not None or c.posted_at is None:
        return
    if job.source != c.source or reason not in ("source_id", "canonical_url"):
        return
    job.posted_at = c.posted_at
    job.posted_at_kind = c.posted_at_kind
    log.log(
        JobLogAction.JOB_POST_DATE_LEARNED,
        job_id=job.id,
        input={"source": c.source, "source_job_id": c.source_job_id},
        output={"posted_at": c.posted_at.isoformat(), "posted_at_kind": c.posted_at_kind},
    )


def _supersedes(duplicate: DuplicateMatch, candidate: JobCandidate) -> bool:
    """A trusted posting must not be folded into an aggregator row that has no trusted date:
    that would drop the trusted timestamp. Only content-level matches qualify (the same id or
    URL is the same record, not a different source's copy)."""
    existing = duplicate.job
    return (
        duplicate.reason in ("fingerprint", "company_title_city")
        and existing.source in AGGREGATOR_SOURCES
        and existing.posted_at is None
        and candidate.source not in AGGREGATOR_SOURCES
        and candidate.posted_at is not None
    )


def link_aggregator_row(
    db: Session,
    aggregator_job: Job,
    trusted_job: Job,
    query: SearchQuery,
    rules: LeadResolutionConfig,
    log: RunLogger,
) -> bool:
    """Link an aggregator job row to the trusted ATS job if the strict same-job rules say they
    are the same posting. The trusted job's fields are untouched (its own timestamp stays
    authoritative); the aggregator row keeps posted_at = NULL, gains duplicate_of_id, and
    becomes DUPLICATE only if it was still DISCOVERED (a rejected row keeps its state)."""
    if aggregator_job.id == trusted_job.id:
        return False
    if aggregator_job.duplicate_of_id is not None:
        return aggregator_job.duplicate_of_id == trusted_job.id
    subject = SameJobSubject(
        title=aggregator_job.title,
        location=aggregator_job.location,
        description=aggregator_job.description,
        first_seen_at=aggregator_job.discovered_at,
        url=aggregator_job.url,
    )
    result = match_same_job(
        subject,
        [trusted_job],
        lambda loc: location_matches(loc, query),
        tolerance=timedelta(hours=rules.match_timing_tolerance_hours),
        min_description_chars=rules.min_description_chars,
        min_overlap=rules.min_description_overlap,
    )
    if result.status != "MATCH":
        return False
    mark_duplicate_of(db, aggregator_job, trusted_job, result.evidence)
    log.log(
        JobLogAction.JOB_SUPERSEDED,
        job_id=aggregator_job.id,
        output={"trusted_job_id": str(trusted_job.id), **result.evidence},
    )
    return True


def mark_duplicate_of(
    db: Session, aggregator_job: Job, trusted_job: Job, evidence: dict[str, Any]
) -> None:
    """aggregator row -> trusted job link (no timestamps move in either direction)."""
    aggregator_job.duplicate_of_id = trusted_job.id
    if aggregator_job.pipeline_state is JobPipelineState.DISCOVERED:
        aggregator_job.pipeline_state = JobPipelineState.DUPLICATE
        db.add(
            ApplicationEvent(
                job_id=aggregator_job.id,
                occurred_at=utcnow(),
                event_type="job_duplicate",
                source=EventSource.AGENT,
                previous_status=JobPipelineState.DISCOVERED.value,
                new_status=JobPipelineState.DUPLICATE.value,
                reason=f"Same posting as trusted {trusted_job.source} job {trusted_job.id}",
                evidence=evidence,
            )
        )
    existing = list(trusted_job.source_attributions or [])
    if not any(a.get("job_id") == str(aggregator_job.id) for a in existing):
        trusted_job.source_attributions = [
            *existing,
            {
                "source": aggregator_job.source,
                "source_job_id": aggregator_job.source_job_id,
                "url": aggregator_job.url,
                "job_id": str(aggregator_job.id),
                "timestamp_trusted": False,
                "first_seen": aggregator_job.discovered_at.isoformat(),
            },
        ]
