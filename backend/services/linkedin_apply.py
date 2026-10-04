"""LinkedIn Easy Apply (Phase D). The user chose to automate it knowing the account risk
(decision 2026-10-04, docs/PROGRESS.md).

    open the job -> "Easy Apply to this job" -> for each step (at most MAX_STEPS):
        read the fields -> fill VERIFIED answers only -> Next / Review
    until "Submit application" shows:
        dry run:  close with Dismiss + Discard. Nothing is sent.
        review:   the form stays open at its last step and waits in the Review queue;
                  only the user's "yes" clicks "Submit application" (submit()).

A required question without a verified answer (or a sensitive one the user never answered)
pauses the job for the user (Needs my input), exactly like the company-site flow; PIE never
guesses. The resume step uploads the job's validated tailored resume in review mode (a dry run
never uploads); without one, LinkedIn's saved resume is used. Field values
are never stored: sessions record labels, answer keys and sources only.
"""

from typing import Any

from sqlalchemy import select

from agent.execution.mapping import option_labels, plan_fields
from backend.services import execution as svc
from backend.services.limits import limit_block
from database.base import utcnow
from database.enums import BrowserExecutionMode, BrowserSessionState, SubmissionOutcome
from database.enums import JobPipelineState as S
from database.models import Application, BrowserSession, Job, SubmissionAttempt
from integrations.browser.manager import BrowserError
from integrations.platforms.base import quota_warning
from integrations.platforms.linkedin import selectors as linkedin

B = BrowserSessionState


def _plans(panel: dict[str, Any], bank: Any, job: Job, resume_ready: bool) -> list[dict[str, Any]]:
    fields = panel.get("fields") or []
    plans = plan_fields(fields, bank, job.location, resume_ready=resume_ready)
    return [{**p.public(), "options": option_labels(f)} for p, f in zip(plans, fields, strict=True)]


def run(ctx: svc.ExecContext, job: Job) -> dict[str, Any]:
    """Dry run (ctx.mode DRY_RUN) or review preparation (LIVE): fill every step and stop
    before "Submit application". Returns a short outcome dict."""
    live = ctx.mode is BrowserExecutionMode.LIVE
    if live:
        svc.check_live(ctx.settings)
    app = ctx.db.scalar(select(Application).where(Application.job_id == job.id))
    if app is None or job.pipeline_state is not S.READY_TO_APPLY:
        return {"outcome": "not_ready"}
    if live:
        reason = limit_block(ctx.db, ctx.settings.automation_path, ctx.settings.timezone, job)
        if reason:
            raise svc.ExecutionError("DAILY_LIMIT_REACHED", reason)
        duplicate = svc.find_duplicate(ctx.db, job, app)
        if duplicate:
            raise svc.ExecutionError("DUPLICATE_APPLICATION", duplicate)
    session = BrowserSession(
        application_id=app.id, job_id=job.id, run_id=ctx.run.id if ctx.run else None,
        mode=ctx.mode, state=B.NAVIGATING, target_url=job.url, target_domain="linkedin.com",
        current_step="Opening Easy Apply", detected_fields=[], filled_fields=[],
        skipped_fields=[], missing_answers=[], challenges=[], screenshots=[], errors=[],
        blocked_requests=[],
    )  # fmt: skip
    ctx.db.add(session)
    ctx.db.flush()
    svc._move(ctx, job, app, S.APPLICATION_IN_PROGRESS, f"Easy Apply started ({ctx.mode.value})")
    ctx.db.commit()
    sid = str(session.id)
    ctx.tool = "linkedin_easy_apply"
    try:
        panel = ctx.browser.li_open(
            sid, job.url, dry_run=not live, button_name=linkedin.EASY_APPLY_BUTTON_NAME
        )
        bank = svc._bank(ctx, app)
        # A validated tailored resume for this job replaces LinkedIn's saved one - uploaded
        # only in review (live) mode: uploading sends the file to LinkedIn. Without one, the
        # resume saved on the user's LinkedIn profile is used.
        version = svc._validated_resume(ctx, app)
        pdf = svc._resume_pdf(ctx, version)
        filled: list[dict[str, Any]] = []
        for step in range(1, linkedin.MAX_STEPS + 1):
            warning = quota_warning(linkedin, str(panel.get("ui_text") or ""))
            if warning:
                raise svc.ExecutionError(
                    "PLATFORM_QUOTA", f"LinkedIn shows a limit/warning ('{warning}'): stopped"
                )
            session.state, session.current_step = B.FILLING, f"Easy Apply step {step}"
            plans = _plans(panel, bank, job, resume_ready=pdf is not None)
            missing = [p for p in plans if p["action"] == "block"]
            for plan in plans:
                if plan["action"] == "upload_resume" and version is not None and pdf is not None:
                    if live:
                        session.uploaded_resume = svc._upload(
                            ctx, session, version, pdf, plan["field_id"]
                        )
                    else:
                        session.uploaded_resume = {
                            "resume_version_id": str(version.id),
                            "version_number": version.version_number,
                            "file_name": pdf.name,
                            "status": "NOT_UPLOADED_DRY_RUN",
                            "detail": "A dry run never uploads: review mode would upload "
                            f"tailored v{version.version_number}.",
                        }
                    continue
                if plan["action"] != "fill":
                    continue
                result = svc._fill_one(ctx, session, plan, bank)
                if result.record:
                    filled.append(result.record)
                elif plan["required"]:
                    missing.append({**plan, "reason": result.reason})
            session.filled_fields = filled
            session.skipped_fields = [
                {k: p[k] for k in ("field_id", "label", "category", "reason")}
                for p in plans
                if p["action"] == "skip"
            ]
            if missing:
                session.missing_answers = [
                    {"field_id": p["field_id"], "question": p["label"],
                     "question_key": p["key"], "category": p["category"],
                     "reason": p["reason"], "answer_status": p.get("answer_status"),
                     "options": p.get("options") or []}  # the page's own choices
                    for p in missing
                ]  # fmt: skip
                questions = "; ".join(m["question"] for m in session.missing_answers)
                svc._block(ctx, session, job, app,
                           f"LinkedIn asks what PIE has no verified answer for: {questions}",
                           {"missing_answers": session.missing_answers})  # fmt: skip
                if not live:
                    ctx.browser.panel_discard(sid, linkedin.DISMISS_LABEL, linkedin.DISCARD_TEXT)
                ctx.db.commit()
                return {"outcome": "needs_input", "session_id": sid}
            if linkedin.SUBMIT_TEXT in panel["buttons"]:
                session.prepared_at = utcnow()
                if live:
                    session.state = B.READY_FOR_SUBMISSION
                    session.current_step = "Filled; waiting for your yes in the review queue"
                    svc._move(ctx, job, app, S.READY_FOR_SUBMISSION, "Easy Apply filled")
                    ctx.db.commit()
                    return {"outcome": "waiting_for_review", "session_id": sid}
                ctx.browser.panel_discard(sid, linkedin.DISMISS_LABEL, linkedin.DISCARD_TEXT)
                session.state, session.closed_at = B.CLOSED, utcnow()
                session.current_step = "Dry run complete: stopped before Submit. Nothing was sent."
                svc._move(ctx, job, app, S.READY_TO_APPLY, "Easy Apply dry run complete")
                svc._event(ctx, job, app, "dry_run_completed", "LinkedIn Easy Apply dry run",
                           {"steps": step, "filled": len(filled)})  # fmt: skip
                ctx.db.commit()
                return {"outcome": "dry_run_complete", "session_id": sid, "steps": step}
            nxt = next((t for t in (linkedin.REVIEW_TEXT, linkedin.NEXT_TEXT)
                        if t in panel["buttons"]), None)  # fmt: skip
            if nxt is None:
                raise svc.ExecutionError("PRECONDITION_FAILED", "No Next / Review / Submit button")
            before = (panel.get("heading"), panel.get("ui_text"))
            panel = ctx.browser.panel_click(sid, nxt)
            if (panel.get("heading"), panel.get("ui_text")) == before:
                raise svc.ExecutionError(
                    "PRECONDITION_FAILED", "The form did not move to the next step (stuck)"
                )
        raise svc.ExecutionError(
            "PRECONDITION_FAILED", f"More than {linkedin.MAX_STEPS} steps: stopped"
        )
    except (BrowserError, svc.ExecutionError) as exc:
        message = exc.message if isinstance(exc, svc.ExecutionError) else f"Browser: {exc}"
        if isinstance(exc, svc.ExecutionError) and exc.code == "PLATFORM_QUOTA":
            # Not this job's fault: close the form, keep the job queued, stop on LinkedIn.
            ctx.browser.panel_discard(sid, linkedin.DISMISS_LABEL, linkedin.DISCARD_TEXT)
            session.state, session.closed_at = B.CLOSED, utcnow()
            session.current_step = message
            svc._move(ctx, job, app, S.READY_TO_APPLY, message)
            svc._event(ctx, job, app, "platform_quota", message)
            ctx.db.commit()
            return {"outcome": "quota", "session_id": sid, "error": message}
        if not live:
            ctx.browser.panel_discard(sid, linkedin.DISMISS_LABEL, linkedin.DISCARD_TEXT)
        svc._fail(ctx, session, job, app, message)
        return {"outcome": "failed", "session_id": sid, "error": message}


def submit(ctx: svc.ExecContext, session: BrowserSession) -> dict[str, Any]:
    """The user's approved LIVE submit. Raises svc.ExecutionError when refused."""
    svc.check_live(ctx.settings)
    job, app = ctx.db.get(Job, session.job_id), ctx.db.get(Application, session.application_id)
    assert job is not None and app is not None
    duplicate = svc.find_duplicate(ctx.db, job, app)
    if duplicate:
        raise svc.ExecutionError("DUPLICATE_APPLICATION", duplicate)
    if (
        session.state is not B.READY_FOR_SUBMISSION
        or job.pipeline_state is not S.READY_FOR_SUBMISSION
    ):
        raise svc.ExecutionError("PRECONDITION_FAILED", "Not waiting for review")
    if session.user_confirmed_at is None:
        raise svc.ExecutionError("USER_CONFIRMATION_REQUIRED", "Only you can approve this")
    reason = limit_block(ctx.db, ctx.settings.automation_path, ctx.settings.timezone, job)
    if reason:
        raise svc.ExecutionError("DAILY_LIMIT_REACHED", reason)
    if not ctx.browser.is_alive(str(session.id)):
        raise svc.ExecutionError(
            "PRECONDITION_FAILED",
            "The prepared LinkedIn form is no longer open (e.g. the backend restarted). Reject "
            "it and run review mode again.",
        )
    attempt = SubmissionAttempt(
        idempotency_key=svc.submission_key(app, job, 0), application_id=app.id,
        session_id=session.id, outcome=SubmissionOutcome.PENDING,
    )  # fmt: skip
    ctx.db.add(attempt)
    ctx.db.commit()  # recorded BEFORE the click: never a second submit
    ctx.tool = "linkedin_submit"
    try:
        after = ctx.browser.panel_submit(str(session.id))
    except BrowserError as exc:
        attempt.outcome, attempt.evidence = SubmissionOutcome.AMBIGUOUS, {"error": str(exc)[:500]}
        svc._block(ctx, session, job, app,
                   f"The Submit click did not complete ({exc}). Check 'My jobs > Applied' on "
                   "LinkedIn, then say in the dashboard whether it was sent. PIE will not retry.",
                   {"attempt": str(attempt.id)})  # fmt: skip
        ctx.db.commit()
        return {"submitted": False, "outcome": "UNKNOWN"}
    evidence = {"sent_message": after.get("sent"), "checked_at": utcnow().isoformat()}
    attempt.evidence = evidence
    if after.get("sent"):
        attempt.outcome = SubmissionOutcome.CONFIRMED
        svc._mark_submitted(
            ctx, session, job, app, evidence, "LinkedIn says the application was sent"
        )
        ctx.db.commit()
        ctx.browser.close(str(session.id))
        return {"submitted": True, "outcome": "CONFIRMED"}
    attempt.outcome = SubmissionOutcome.AMBIGUOUS
    svc._block(ctx, session, job, app,
               "LinkedIn did not clearly confirm the application. Check 'My jobs > Applied' on "
               "LinkedIn, then say in the dashboard whether it was sent. PIE will not retry.",
               {"evidence": evidence})  # fmt: skip
    ctx.db.commit()
    return {"submitted": False, "outcome": "AMBIGUOUS"}
