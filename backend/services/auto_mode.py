"""Auto mode (REQUIREMENTS 10, Phase E3): submit without asking per application.

The user asked for it on 2026-10-04 ("make it fully automatic"). REQUIREMENTS 10 sets the
gate, per platform: auto unlocks only after that platform has had
  1. a completed dry run, and
  2. a real application the user approved in review mode (confirmed sent).
Starting an auto run also needs the live switches (DEVELOPMENT_MODE=false,
EXTERNAL_LIVE_ENABLED=true, a visible browser) and the typed phrase
"AUTO on <platform>: up to N today" - that one confirmation stands in for the per-application
"yes". Everything else stays: daily limits (checked before every click), duplicates, the
quota/warning stop, and questions without a verified answer pause the job (never guessed).
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.services import execution as svc
from backend.services.platforms import Platform, platform_of
from database.enums import BrowserExecutionMode, BrowserSessionState, SubmissionOutcome
from database.models import Application, ApplicationEvent, BrowserSession, Job, SubmissionAttempt

DRY_RUN_EVENTS = ("dry_run_completed", "naukri_dry_run_checked", "instahyre_dry_run_checked")
APPROVED = (SubmissionOutcome.CONFIRMED, SubmissionOutcome.USER_CONFIRMED)
AUTO_PREFIX = "AUTO MODE (one confirmation for this run): "


def auto_status(db: Session, platform: Platform) -> dict[str, Any]:
    """Whether auto is unlocked on a platform, and what is still missing."""
    dry = any(
        platform_of(job) == platform
        for job in db.scalars(
            select(Job)
            .join(ApplicationEvent, ApplicationEvent.job_id == Job.id)
            .where(ApplicationEvent.event_type.in_(DRY_RUN_EVENTS))
        ).all()
    )
    reviewed = any(
        platform_of(job) == platform and session.user_confirmed_at is not None
        and not (session.user_confirmation or "").startswith(AUTO_PREFIX)
        for job, session in db.execute(
            select(Job, BrowserSession)
            .join(BrowserSession, BrowserSession.job_id == Job.id)
            .join(SubmissionAttempt, SubmissionAttempt.session_id == BrowserSession.id)
            .where(SubmissionAttempt.outcome.in_(APPROVED))
        ).all()
    )  # fmt: skip
    missing = []
    if not dry:
        missing.append("one completed dry run")
    if not reviewed:
        missing.append("one application you approved in review mode")
    return {"platform": platform, "unlocked": not missing, "missing": missing}


def auto_submit(
    db: Session, settings: Any, profile: Any, job: Job, browser: Any, platform_browser: Any
) -> dict[str, Any]:
    """Submit a job that was just prepared (READY_FOR_SUBMISSION) in an auto run, through the
    same submit code (and checks) as an approval in the review queue."""
    from backend.api.routes.applications import confirmation_statement

    app = db.scalar(select(Application).where(Application.job_id == job.id))
    session = db.scalars(
        select(BrowserSession)
        .where(
            BrowserSession.application_id == (app.id if app else None),
            BrowserSession.mode == BrowserExecutionMode.LIVE,
            BrowserSession.state == BrowserSessionState.READY_FOR_SUBMISSION,
        )
        .order_by(BrowserSession.created_at.desc())
    ).first()
    if app is None or session is None:
        return {"submitted": False, "outcome": "not_prepared"}
    svc.confirm_live_submission(db, settings, session.id, AUTO_PREFIX + confirmation_statement(job))
    db.refresh(session)
    platform = platform_of(job)
    from backend.services.naukri_apply import ONE_CLICK

    if platform in ONE_CLICK:  # Naukri, Instahyre
        from backend.services import naukri_apply

        return naukri_apply.submit(
            db, settings, profile, platform_browser(platform), session, ONE_CLICK[platform]
        )
    ctx = svc.ExecContext(
        db=db, settings=settings, profile=profile,
        browser=platform_browser("linkedin") if platform == "linkedin" else browser,
        mode=BrowserExecutionMode.LIVE, actor="agent", tool="auto_submit",
    )  # fmt: skip
    if platform == "linkedin":
        from backend.services import linkedin_apply

        return linkedin_apply.submit(ctx, session)
    out = svc.submit(ctx, session.id)
    return {"submitted": bool(out.get("submitted")), "outcome": out.get("outcome")}
