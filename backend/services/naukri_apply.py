"""Naukri applying, REVIEW MODE ONLY (decision 2026-10-04, docs/PROGRESS.md).

Naukri's "Apply" button sends the user's Naukri profile at once, so there is nothing to fill
or preview first, and no dry run beyond "this job would be applied to".

    prepare  (Runs page, review mode): open the job page (read only) and see which button it
             has. "Apply" -> the job waits in the Review queue. "Apply on company site" ->
             the manual-apply list with that reason. Already "Applied" -> withdrawn.
    approve  (Review queue, the user's "yes"): every live check (live switches, duplicates,
             daily limits incl. Naukri's own), a submission attempt recorded BEFORE the click,
             then exactly one click on a button that must read "Apply".
             - the button turns to "Applied" (or a success message): SUBMITTED;
             - Naukri asks follow-up questions, or nothing clear happens: the job waits for
               the user, who answers in the open browser window (true facts only) and then
               says in the dashboard whether it was submitted. PIE never retries the click.

Naukri sends the resume saved on the user's Naukri profile; tailored resumes are not used.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.config import Settings
from backend.core.profile import UserProfile
from backend.services import execution as svc
from backend.services.limits import limit_block
from backend.services.manual_apply import send_to_manual
from database.base import utcnow
from database.enums import (
    BrowserExecutionMode,
    BrowserSessionState,
    EventSource,
    SubmissionOutcome,
)
from database.enums import JobPipelineState as S
from database.models import Application, ApplicationEvent, BrowserSession, Job, SubmissionAttempt
from database.state_machine import assert_transition
from integrations.browser.manager import BrowserError
from integrations.platforms.base import classify_page
from integrations.platforms.naukri import selectors as naukri

READY_STEP = "Ready: approving sends your Naukri profile and its saved resume"


def _move(db: Session, job: Job, app: Application, target: S, event: str, reason: str) -> None:
    previous = job.pipeline_state
    assert_transition(previous, target)
    job.pipeline_state = target
    db.add(
        ApplicationEvent(
            job_id=job.id, application_id=app.id, event_type=event, source=EventSource.SYSTEM,
            previous_status=previous.value, new_status=target.value, reason=reason,
        )
    )  # fmt: skip


def prepare(
    db: Session, browser: Any, job: Job, run_id: Any = None, *, dry_run: bool = False
) -> str:
    """Read the job page and route the job. Returns waiting_for_review | manual |
    already_applied | blocked | login_needed | unknown. Nothing is clicked. A dry run only
    reports what would happen ("would_" + outcome) and changes nothing."""
    app = db.scalar(select(Application).where(Application.job_id == job.id))
    if app is None or job.pipeline_state is not S.READY_TO_APPLY:
        return "unknown"
    res = browser.read_cards(job.url, naukri.APPLY_SPEC)
    check = classify_page(
        naukri, str(res.get("url") or ""), res.get("status"), str(res.get("text") or "")
    )
    if check.state == "blocked":
        return "blocked"
    if any(p in str(res.get("url") or "").lower() for p in naukri.LOGGED_OUT_URL_PARTS):
        return "login_needed"
    buttons = {i["id"]: " ".join(str(i.get("text") or "").split()) for i in res.get("items") or []}
    apply_text = buttons.get(naukri.APPLY_BUTTON.lstrip("#"))
    if dry_run:
        if apply_text is not None and apply_text.lower() == "apply":
            return "would_wait_for_review"
        if apply_text is not None and apply_text.lower().startswith("applied"):
            return "would_skip_already_applied"
        if naukri.COMPANY_SITE_BUTTON.lstrip("#") in buttons:
            return "would_go_to_manual_apply"
        return "unknown"
    if apply_text is not None:
        if apply_text.lower().startswith("applied"):
            _move(db, job, app, S.WITHDRAWN, "already_applied_on_naukri",
                  "Naukri shows this job as already applied (outside PIE)")  # fmt: skip
            db.commit()
            return "already_applied"
        if apply_text.lower() != "apply":
            return "unknown"
        session = BrowserSession(
            application_id=app.id, job_id=job.id, run_id=run_id, mode=BrowserExecutionMode.LIVE,
            state=BrowserSessionState.READY_FOR_SUBMISSION, target_url=job.url,
            target_domain="naukri.com", current_url=job.url, current_step=READY_STEP,
            detected_fields=[], filled_fields=[], skipped_fields=[], missing_answers=[],
            challenges=[], screenshots=[], errors=[], blocked_requests=[], prepared_at=utcnow(),
        )  # fmt: skip
        db.add(session)
        _move(db, job, app, S.APPLICATION_IN_PROGRESS, "naukri_prepared", "Naukri job checked")
        _move(db, job, app, S.READY_FOR_SUBMISSION, "naukri_waiting_for_review",
              "Has Naukri's Apply button: waiting for your yes/no in the review queue")  # fmt: skip
        db.commit()
        return "waiting_for_review"
    if naukri.COMPANY_SITE_BUTTON.lstrip("#") in buttons:
        send_to_manual(db, job.id, "Naukri sends you to the company's own site for this job",
                       EventSource.SYSTEM)  # fmt: skip
        return "manual"
    return "unknown"


def submit(
    db: Session,
    settings: Settings,
    profile: UserProfile,
    browser: Any,
    session: BrowserSession,
) -> dict[str, Any]:
    """The user's approved, LIVE Naukri apply. Raises svc.ExecutionError when refused."""
    svc.check_live(settings)
    job, app = db.get(Job, session.job_id), db.get(Application, session.application_id)
    assert job is not None and app is not None
    ctx = svc.ExecContext(
        db=db, settings=settings, profile=profile, browser=browser,
        mode=BrowserExecutionMode.LIVE, actor="user", tool="naukri_apply",
    )  # fmt: skip
    duplicate = svc.find_duplicate(db, job, app)
    if duplicate:
        raise svc.ExecutionError("DUPLICATE_APPLICATION", duplicate)
    if job.pipeline_state is not S.READY_FOR_SUBMISSION:
        raise svc.ExecutionError("PRECONDITION_FAILED", f"Job is {job.pipeline_state.value}")
    if session.state is not BrowserSessionState.READY_FOR_SUBMISSION:
        raise svc.ExecutionError("PRECONDITION_FAILED", "This Naukri job is not waiting for review")
    if session.user_confirmed_at is None:
        raise svc.ExecutionError("USER_CONFIRMATION_REQUIRED", "Only you can approve this")
    reason = limit_block(db, settings.automation_path, settings.timezone, job)
    if reason:
        raise svc.ExecutionError("DAILY_LIMIT_REACHED", reason)

    attempt = SubmissionAttempt(
        idempotency_key=svc.submission_key(app, job, 0), application_id=app.id,
        session_id=session.id, outcome=SubmissionOutcome.PENDING,
    )  # fmt: skip
    db.add(attempt)
    db.commit()  # recorded BEFORE the click: a crash can never lead to a second click
    try:
        out = browser.click_platform_apply(
            job.url, naukri.APPLY_BUTTON, "Apply", naukri.FOLLOW_UP_MARKER
        )
    except BrowserError as exc:
        attempt.outcome = SubmissionOutcome.AMBIGUOUS
        attempt.evidence = {"error": str(exc)[:500]}
        message = (
            f"The Apply click did not complete ({exc}). Whether Naukri received it is "
            "UNKNOWN - PIE will not click again. Check 'Applied jobs' on Naukri, then say in "
            "the dashboard whether it was submitted."
        )
        svc._block(ctx, session, job, app, message, {"attempt": str(attempt.id)})
        db.commit()
        return {"submitted": False, "outcome": "UNKNOWN", "state": session.state.value}

    after = (out.get("button_after") or "").lower()
    text = (out.get("text") or "").lower()
    evidence = {"button_after": out.get("button_after"), "follow_up": out.get("follow_up"),
                "url_after": out.get("url"), "checked_at": utcnow().isoformat()}  # fmt: skip
    attempt.evidence = evidence
    if not out.get("follow_up") and (
        after.startswith("applied") or any(t in text for t in naukri.APPLIED_TEXTS[1:])
    ):
        attempt.outcome = SubmissionOutcome.CONFIRMED
        svc._mark_submitted(ctx, session, job, app, evidence, "Naukri shows the job as applied")
        db.commit()
        return {"submitted": True, "outcome": "CONFIRMED", "state": session.state.value}
    attempt.outcome = SubmissionOutcome.AMBIGUOUS
    message = (
        "Naukri is asking follow-up questions in the open browser window. Answer them there "
        "(true facts only), then say in the dashboard whether the application was sent."
        if out.get("follow_up")
        else "Naukri did not clearly confirm the application. Check 'Applied jobs' on Naukri, "
        "then say in the dashboard whether it was sent. PIE will not click again."
    )
    svc._block(ctx, session, job, app, message, {"evidence": evidence})
    db.commit()
    return {"submitted": False, "outcome": "AMBIGUOUS", "state": session.state.value}
