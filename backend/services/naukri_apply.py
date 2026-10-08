"""One-click platform applying: Naukri and Instahyre (``sel`` = the platform's selectors).

Their apply button ("Apply" on Naukri, "Apply now" on Instahyre) sends the user's profile on
that platform at once, so there is nothing to fill or preview first, and no dry run beyond
"this job would be applied to".

    prepare  (Runs page): open the job page (read only) and see which button it has. The apply
             button -> the job waits in the Review queue. Naukri's "Apply on company site" ->
             the manual-apply list with that reason. Already applied -> withdrawn.
    approve  (Review queue, the user's "yes", or an unlocked auto run): every live check (live
             switches, duplicates, daily limits incl. the platform's own), a submission
             attempt recorded BEFORE the click, then exactly one click on a button that must
             read the apply text.
             - the button turns to the applied text (or a success message): SUBMITTED;
             - follow-up questions, or nothing clear happens: the job waits for the user, who
               answers in the open browser window (true facts only) and then says in the
               dashboard whether it was submitted. PIE never retries the click.

The platform sends the resume saved on the user's profile there; tailored resumes are not used.
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
from integrations.platforms.base import classify_page, quota_warning
from integrations.platforms.instahyre import selectors as instahyre
from integrations.platforms.naukri import selectors as naukri

# Platforms applied to with one click, by platform name.
ONE_CLICK: dict[str, Any] = {"naukri": naukri, "instahyre": instahyre}


def ready_step(sel: Any) -> str:
    return f"Ready: approving sends your {sel.NAME} profile and its saved resume"


READY_STEP = ready_step(naukri)


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


def _apply_text(sel: Any, items: list[dict[str, Any]]) -> str | None:
    """The apply (or already-applied) button's text among the read buttons, or None."""
    for item in items:
        if sel.COMPANY_SITE_ID and item.get("id") == sel.COMPANY_SITE_ID:
            continue
        text = " ".join(str(item.get("text") or "").split())
        low = text.lower()
        if low == sel.APPLY_TEXT.lower() or low.startswith(sel.APPLIED_PREFIX):
            return text
    return None


def prepare(
    db: Session,
    browser: Any,
    job: Job,
    run_id: Any = None,
    *,
    dry_run: bool = False,
    sel: Any = naukri,
) -> str:
    """Read the job page and route the job. Returns waiting_for_review | manual |
    already_applied | blocked | login_needed | unknown. Nothing is clicked. A dry run only
    reports what would happen ("would_" + outcome) and changes nothing."""
    app = db.scalar(select(Application).where(Application.job_id == job.id))
    if app is None or job.pipeline_state is not S.READY_TO_APPLY:
        return "unknown"
    res = browser.read_cards(job.url, sel.APPLY_SPEC)
    text = str(res.get("text") or "")
    check = classify_page(sel, str(res.get("url") or ""), res.get("status"), text)
    if check.state == "blocked":
        return "blocked"
    if quota_warning(sel, text):
        return "quota"
    if any(p in str(res.get("url") or "").lower() for p in sel.LOGGED_OUT_URL_PARTS):
        return "login_needed"
    items = list(res.get("items") or [])
    apply_text = _apply_text(sel, items)
    company_site = bool(sel.COMPANY_SITE_ID) and any(
        i.get("id") == sel.COMPANY_SITE_ID for i in items
    )
    is_apply = apply_text is not None and apply_text.lower() == sel.APPLY_TEXT.lower()
    is_applied = apply_text is not None and apply_text.lower().startswith(sel.APPLIED_PREFIX)
    if dry_run:
        if is_apply:
            db.add(
                ApplicationEvent(
                    job_id=job.id, application_id=app.id,
                    event_type=f"{sel.PLATFORM}_dry_run_checked", source=EventSource.SYSTEM,
                    reason=f"Dry run: has {sel.NAME}'s {sel.APPLY_TEXT} button",
                )
            )  # fmt: skip
            db.commit()
            return "would_wait_for_review"
        if is_applied:
            return "would_skip_already_applied"
        if company_site:
            return "would_go_to_manual_apply"
        return "unknown"
    if is_applied:
        _move(db, job, app, S.WITHDRAWN, f"already_applied_on_{sel.PLATFORM}",
              f"{sel.NAME} shows this job as already applied (outside PIE)")  # fmt: skip
        db.commit()
        return "already_applied"
    if is_apply:
        session = BrowserSession(
            application_id=app.id, job_id=job.id, run_id=run_id, mode=BrowserExecutionMode.LIVE,
            state=BrowserSessionState.READY_FOR_SUBMISSION, target_url=job.url,
            target_domain=sel.DOMAIN, current_url=job.url, current_step=ready_step(sel),
            detected_fields=[], filled_fields=[], skipped_fields=[], missing_answers=[],
            challenges=[], screenshots=[], errors=[], blocked_requests=[], prepared_at=utcnow(),
        )  # fmt: skip
        db.add(session)
        _move(db, job, app, S.APPLICATION_IN_PROGRESS, f"{sel.PLATFORM}_prepared",
              f"{sel.NAME} job checked")  # fmt: skip
        _move(db, job, app, S.READY_FOR_SUBMISSION, f"{sel.PLATFORM}_waiting_for_review",
              f"Has {sel.NAME}'s {sel.APPLY_TEXT} button: waiting for your yes/no in the "
              "review queue")  # fmt: skip
        db.commit()
        return "waiting_for_review"
    if company_site:
        send_to_manual(db, job.id, f"{sel.NAME} sends you to the company's own site for this job",
                       EventSource.SYSTEM)  # fmt: skip
        return "manual"
    return "unknown"


def submit(
    db: Session,
    settings: Settings,
    profile: UserProfile,
    browser: Any,
    session: BrowserSession,
    sel: Any = naukri,
) -> dict[str, Any]:
    """The user's approved (or auto-run), LIVE one-click apply. Raises svc.ExecutionError
    when refused."""
    svc.check_live(settings)
    job, app = db.get(Job, session.job_id), db.get(Application, session.application_id)
    assert job is not None and app is not None
    ctx = svc.ExecContext(
        db=db, settings=settings, profile=profile, browser=browser,
        mode=BrowserExecutionMode.LIVE, actor="user", tool=f"{sel.PLATFORM}_apply",
    )  # fmt: skip
    duplicate = svc.find_duplicate(db, job, app)
    if duplicate:
        raise svc.ExecutionError("DUPLICATE_APPLICATION", duplicate)
    if job.pipeline_state is not S.READY_FOR_SUBMISSION:
        raise svc.ExecutionError("PRECONDITION_FAILED", f"Job is {job.pipeline_state.value}")
    if session.state is not BrowserSessionState.READY_FOR_SUBMISSION:
        raise svc.ExecutionError(
            "PRECONDITION_FAILED", f"This {sel.NAME} job is not waiting for review"
        )
    if session.user_confirmed_at is None:
        raise svc.ExecutionError("USER_CONFIRMATION_REQUIRED", "Only you can approve this")
    reason = limit_block(db, settings.automation_path, settings.timezone, job)
    if reason:
        raise svc.ExecutionError("DAILY_LIMIT_REACHED", reason)

    attempt = SubmissionAttempt(
        idempotency_key=svc.submission_key(app, job, svc.next_generation(db, app)),
        application_id=app.id,
        session_id=session.id, outcome=SubmissionOutcome.PENDING,
    )  # fmt: skip
    db.add(attempt)
    db.commit()  # recorded BEFORE the click: a crash can never lead to a second click
    try:
        out = browser.click_platform_apply(
            job.url, sel.APPLY_BUTTON, sel.APPLY_TEXT, sel.FOLLOW_UP_MARKER,
            sel.APPLIED_BUTTON_TEXT,
        )  # fmt: skip
    except BrowserError as exc:
        if exc.kind == "NOT_CLICKED":
            # Stopped BEFORE the click (button covered or changed): certainly nothing was
            # sent, so the attempt is dropped and the job goes back in the queue.
            db.delete(attempt)
            session.state, session.closed_at = BrowserSessionState.CLOSED, utcnow()
            session.current_step = f"Not clicked: {exc}"[:500]
            session.errors = [*(session.errors or []), {"step": "apply", "error": str(exc)[:500]}]
            _move(db, job, app, S.READY_TO_APPLY, f"{sel.PLATFORM}_not_clicked",
                  f"PIE did not click {sel.APPLY_TEXT} ({exc}); nothing was sent")  # fmt: skip
            db.commit()
            return {"submitted": False, "outcome": "NOT_CLICKED", "state": session.state.value}
        attempt.outcome = SubmissionOutcome.AMBIGUOUS
        attempt.evidence = {"error": str(exc)[:2000]}
        message = (
            f"The {sel.APPLY_TEXT} click did not complete ({exc}). Whether {sel.NAME} received "
            f"it is UNKNOWN - PIE will not click again. Check your applied jobs on {sel.NAME}, "
            "then say in the dashboard whether it was submitted."
        )
        svc._block(ctx, session, job, app, message, {"attempt": str(attempt.id)})
        db.commit()
        return {"submitted": False, "outcome": "UNKNOWN", "state": session.state.value}

    warning = quota_warning(sel, str(out.get("text") or ""))
    if warning:
        attempt.outcome = SubmissionOutcome.AMBIGUOUS
        attempt.evidence = {"quota_warning": warning}
        svc._block(ctx, session, job, app,
                   f"{sel.NAME} shows a limit/warning ('{warning}'). PIE stopped applying on "
                   f"{sel.NAME}. Check your applied jobs there and say whether this one was sent.",
                   {"quota_warning": warning})  # fmt: skip
        db.commit()
        return {"submitted": False, "outcome": "QUOTA", "state": session.state.value}
    after = (out.get("button_after") or "").lower()
    text = (out.get("text") or "").lower()
    evidence = {"button_after": out.get("button_after"), "follow_up": out.get("follow_up"),
                "url_after": out.get("url"), "checked_at": utcnow().isoformat()}  # fmt: skip
    attempt.evidence = evidence
    # The platform's own applied button is the proof; a chat drawer can be on the page too.
    if out.get("applied") or (
        not out.get("follow_up")
        and (after.startswith(sel.APPLIED_PREFIX) or any(t in text for t in sel.APPLIED_TEXTS[1:]))
    ):
        attempt.outcome = SubmissionOutcome.CONFIRMED
        svc._mark_submitted(ctx, session, job, app, evidence,
                            f"{sel.NAME} shows the job as applied")  # fmt: skip
        db.commit()
        return {"submitted": True, "outcome": "CONFIRMED", "state": session.state.value}
    attempt.outcome = SubmissionOutcome.AMBIGUOUS
    message = (
        f"{sel.NAME} is asking follow-up questions in the open browser window. Answer them "
        "there (true facts only), then say in the dashboard whether the application was sent."
        if out.get("follow_up")
        else f"{sel.NAME} did not clearly confirm the application. Check your applied jobs on "
        f"{sel.NAME}, then say in the dashboard whether it was sent. PIE will not click again."
    )
    svc._block(ctx, session, job, app, message, {"evidence": evidence})
    db.commit()
    return {"submitted": False, "outcome": "AMBIGUOUS", "state": session.state.value}
