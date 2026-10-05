"""Aggregates for the dashboard overview, computed from the database."""

from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.config import REPO_ROOT, Settings
from database.enums import ApplicationStatus as AS
from database.enums import EmailClassification as EC
from database.enums import JobPipelineState as JS
from database.models import AgentRun, Application, ApplicationEvent, Email, Job


class FunnelStage(BaseModel):
    stage: str
    count: int


class ActivityItem(BaseModel):
    occurred_at: datetime
    event_type: str
    reason: str | None
    new_status: str | None


class AgentSnapshot(BaseModel):
    status: str
    task: str | None
    started_at: datetime | None
    jobs_processed: int
    applications_submitted: int
    error_count: int


class DashboardOverview(BaseModel):
    jobs_discovered_today: int
    jobs_shortlisted: int
    applications_submitted: int
    assessments: int
    interviews: int
    recruiter_responses: int
    offers: int
    rejections: int
    funnel: list[FunnelStage]
    recent_activity: list[ActivityItem]
    agent: AgentSnapshot


# Statuses that imply an application was (confirmed) submitted and reached this stage or later.
_APPLIED_OR_LATER = {
    AS.APPLIED,
    AS.APPLICATION_CONFIRMED,
    AS.ASSESSMENT,
    AS.RECRUITER_CONTACT,
    AS.INTERVIEW,
    AS.FINAL_ROUND,
    AS.OFFER,
    AS.REJECTED,
    AS.NO_RESPONSE,
}
_INTERVIEW_OR_LATER = {AS.INTERVIEW, AS.FINAL_ROUND, AS.OFFER}
_SHORTLISTED_OR_LATER = {
    JS.SHORTLISTED,
    JS.READY_TO_APPLY,
    JS.APPLICATION_IN_PROGRESS,
    JS.SUBMITTED,
    JS.BLOCKED_NEEDS_USER,
}


def _count(db: Session, model: type, *where: object) -> int:
    return db.scalar(select(func.count()).select_from(model).where(*where)) or 0  # type: ignore[arg-type]


def _events_reached(db: Session, statuses: set[AS]) -> int:
    """Distinct applications that ever entered one of `statuses` (from the timeline)."""
    stmt = select(func.count(func.distinct(ApplicationEvent.application_id))).where(
        ApplicationEvent.new_status.in_([s.value for s in statuses])
    )
    return db.scalar(stmt) or 0


def build_overview(
    db: Session, now: datetime | None = None, tz: str = "Asia/Kolkata"
) -> DashboardOverview:
    """`tz` is the user's timezone; "today" means today on their calendar, not UTC's."""
    zone = ZoneInfo(tz)
    local_now = (now or datetime.now(UTC)).astimezone(zone)
    start_of_day = datetime.combine(local_now.date(), time.min, tzinfo=zone)

    discovered_total = _count(db, Job)
    shortlisted = _count(db, Job, Job.pipeline_state.in_(_SHORTLISTED_OR_LATER))
    applied = _count(db, Application, Application.status.in_(_APPLIED_OR_LATER))
    assessments = _events_reached(db, {AS.ASSESSMENT})
    interviews = _events_reached(db, _INTERVIEW_OR_LATER)
    offers = _count(db, Application, Application.status == AS.OFFER)

    latest_run = db.scalar(select(AgentRun).order_by(AgentRun.started_at.desc()).limit(1))
    recent = db.scalars(
        select(ApplicationEvent).order_by(ApplicationEvent.occurred_at.desc()).limit(10)
    ).all()

    return DashboardOverview(
        jobs_discovered_today=_count(db, Job, Job.discovered_at >= start_of_day),
        jobs_shortlisted=shortlisted,
        applications_submitted=applied,
        assessments=assessments,
        interviews=interviews,
        recruiter_responses=_count(db, Email, Email.classification == EC.RECRUITER),
        offers=offers,
        rejections=_count(db, Application, Application.status == AS.REJECTED),
        funnel=[
            FunnelStage(stage="Discovered", count=discovered_total),
            FunnelStage(stage="Shortlisted", count=shortlisted),
            FunnelStage(stage="Applied", count=applied),
            FunnelStage(stage="Assessment", count=assessments),
            FunnelStage(stage="Interview", count=interviews),
            FunnelStage(stage="Offer", count=offers),
        ],
        recent_activity=[
            ActivityItem(
                occurred_at=e.occurred_at,
                event_type=e.event_type,
                reason=e.reason,
                new_status=e.new_status,
            )
            for e in recent
        ],
        agent=AgentSnapshot(
            status=latest_run.status.value if latest_run else "IDLE",
            task=latest_run.task if latest_run else None,
            started_at=latest_run.started_at if latest_run else None,
            jobs_processed=latest_run.jobs_processed if latest_run else 0,
            applications_submitted=latest_run.applications_submitted if latest_run else 0,
            error_count=len(latest_run.errors) if latest_run else 0,
        ),
    )


# --- Home page (REQUIREMENTS 19.2.1) -------------------------------------------------------


class PlatformMode(BaseModel):
    platform: str  # naukri | linkedin | company_site
    mode: str  # dry-run | review | auto
    built: bool  # False: the platform module comes in a later phase
    note: str | None
    login: str | None  # logged_in | expired | never | blocked; None = not applicable / not built
    login_command: str | None = None  # how to log in (again)


class StatusCounts(BaseModel):
    found: int
    skipped: int
    applied: int
    needs_input: int
    manual: int
    failed: int


class QuickLinks(BaseModel):
    needs_input: int
    failed: int
    manual: int | None
    waiting_for_review: int | None


class HomeStatus(BaseModel):
    live_possible: bool  # True: DEVELOPMENT_MODE=false and EXTERNAL_LIVE_ENABLED=true
    live_note: str
    platforms: list[PlatformMode]
    today: StatusCounts
    all_time: StatusCounts
    quick_links: QuickLinks


def _waiting_for_review(db: Session) -> int:
    from database.enums import BrowserExecutionMode, BrowserSessionState
    from database.models import BrowserSession

    return _count(
        db,
        BrowserSession,
        BrowserSession.mode == BrowserExecutionMode.LIVE,
        BrowserSession.state == BrowserSessionState.READY_FOR_SUBMISSION,
    )


def _status_counts(db: Session, *where: object) -> StatusCounts:
    from backend.services.statuses import USER_STATUSES, user_status

    rows = db.execute(
        select(Job.pipeline_state, func.count()).where(*where).group_by(Job.pipeline_state)  # type: ignore[arg-type]
    ).all()
    counts = dict.fromkeys(USER_STATUSES, 0)
    for state, n in rows:
        counts[user_status(state)] += n
    return StatusCounts(**counts)


def build_home(
    db: Session, settings: Settings, now: datetime | None = None, data_dir: Path | None = None
) -> HomeStatus:
    """Facts only. Platform modes are dry-run until review/auto exist (Phase C / E)."""
    from backend.core.config import live_execution_block

    zone = ZoneInfo(settings.timezone)
    local_now = (now or datetime.now(UTC)).astimezone(zone)
    start_of_day = datetime.combine(local_now.date(), time.min, tzinfo=zone)
    block = live_execution_block(settings)
    from integrations.platforms.base import load_status

    logins = load_status(data_dir or REPO_ROOT / "data")
    platforms = [
        PlatformMode(
            platform="naukri",
            mode="dry-run",
            built=False,
            note="Login check works; search and apply come after the live login test.",
            login=(logins.get("naukri") or {}).get("state", "never"),
            login_command="python -m backend.scripts.login naukri --test-search",
        ),
        PlatformMode(
            platform="linkedin",
            mode="dry-run",
            built=False,
            note="Login check works; search and Easy Apply come after the live login test.",
            login=(logins.get("linkedin") or {}).get("state", "never"),
            login_command="python -m backend.scripts.login linkedin --test-search",
        ),
        PlatformMode(
            platform="instahyre",
            mode="dry-run",
            built=False,
            note="Search works; applying comes next (dry run, then one application you approve).",
            login=(logins.get("instahyre") or {}).get("state", "never"),
            login_command="python -m backend.scripts.login instahyre",
        ),
        PlatformMode(
            platform="company_site",
            mode="dry-run",
            built=True,
            note="Live submits still need your confirmation of each prepared form.",
            login=None,
        ),
    ]
    today = _status_counts(db, Job.updated_at >= start_of_day)
    all_time = _status_counts(db)
    return HomeStatus(
        live_possible=block is None,
        live_note=(
            "LIVE submissions are switched ON (DEVELOPMENT_MODE=false, EXTERNAL_LIVE_ENABLED=true)."
            if block is None
            else block[1]
        ),
        platforms=platforms,
        today=today,
        all_time=all_time,
        quick_links=QuickLinks(
            needs_input=all_time.needs_input,
            failed=all_time.failed,
            manual=all_time.manual,
            waiting_for_review=_waiting_for_review(db),
        ),
    )
