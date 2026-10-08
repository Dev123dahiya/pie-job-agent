"""Application execution in a real browser, behind PIE's gates.

    READY_TO_APPLY -> APPLICATION_IN_PROGRESS -> READY_FOR_SUBMISSION -> SUBMITTED

- DRY_RUN (the default) performs everything except the final submit and, as a safety net,
  the browser aborts every non-GET request the page makes. A dry run can never produce
  SUBMITTED: no dry-run code path calls the submit click or writes a SubmissionAttempt.
- LIVE is refused with DEVELOPMENT_MODE_BLOCKED whenever DEVELOPMENT_MODE=true (whatever
  EXTERNAL_LIVE_ENABLED says), with LIVE_NOT_ENABLED unless EXTERNAL_LIVE_ENABLED=true, and
  with VISIBLE_BROWSER_REQUIRED while the browser is headless. These checks run before any
  browser code. A live submission also needs the user's explicit confirmation of the
  prepared form; then five checks run (database duplicates, application state, browser state,
  existing evidence, confirmation) and a SubmissionAttempt with a unique idempotency key is
  committed before the click, so the final submit can never happen twice.
- Only VERIFIED_AUTOFILL fields are filled (see agent/execution/mapping.py). Unknown,
  ambiguous and sensitive questions stop for the user; so do CAPTCHA, OTP, 2FA, identity
  checks and login walls. PIE never solves, bypasses or guesses.
- SUBMITTED needs positive evidence (confirmation text / application id). Anything less is
  ambiguous: the job is BLOCKED_NEEDS_USER and nothing is retried.
- A page lost to a crash or a backend restart is reloaded and the form state is rebuilt from
  the stored record (the same verified answers are re-entered and re-checked).

Every external action is written to external_action_logs. No passwords, cookies or tokens are
stored or logged; URLs are redacted and screenshots mask password/one-time-code inputs.
"""

import contextlib
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent.execution.answers import Answer, AnswerBank
from agent.execution.mapping import (
    AMBIGUOUS,
    VERIFIED_AUTOFILL,
    choose_option,
    fill_target,
    option_labels,
    plan_fields,
)
from backend.core.config import Settings, live_execution_block
from backend.core.profile import UserProfile
from backend.services.limits import limit_block
from backend.services.readiness import evaluate_readiness, user_blockers
from database.base import utcnow
from database.enums import (
    AnswerStatus,
    ApplicationStatus,
    BrowserExecutionMode,
    BrowserSessionState,
    EventSource,
    ExternalActionResult,
    JobPipelineState,
    ResumeValidationStatus,
    SubmissionOutcome,
)
from database.models import (
    AgentRun,
    Application,
    ApplicationAnswer,
    ApplicationEvent,
    BrowserSession,
    ExternalActionLog,
    Job,
    ResumeVersion,
    SubmissionAttempt,
)
from database.state_machine import assert_transition
from integrations.browser.manager import BrowserError, BrowserManager, Snapshot
from integrations.browser.signals import classify_submission, detect_challenges, domain_of

logger = logging.getLogger(__name__)

S = JobPipelineState
B = BrowserSessionState
DRY_RUN_BANNER = "DRY RUN — Nothing has been submitted."
LIVE_BANNER = "LIVE EXTERNAL ACTION"
ACTIVE = {
    B.CREATED,
    B.OPEN,
    B.NAVIGATING,
    B.FORM_DETECTED,
    B.FILLING,
    B.WAITING_FOR_USER,
    B.READY_FOR_SUBMISSION,
}
# Attempts that mean "this application may already be with the employer".
BLOCKING_OUTCOMES = {
    SubmissionOutcome.PENDING,
    SubmissionOutcome.CONFIRMED,
    SubmissionOutcome.AMBIGUOUS,
    SubmissionOutcome.FAILED,
    SubmissionOutcome.USER_CONFIRMED,
}
# Resume upload outcomes. A dry run never claims the employer received the file.
UPLOAD_SUCCESS = "UPLOAD_SUCCESS"  # live: the input holds the file, no error shown
UPLOAD_UI_SUCCESS = "UPLOAD_UI_SUCCESS"  # dry run: the input holds the file, nothing was sent
UPLOAD_BLOCKED = "UPLOAD_REQUEST_BLOCKED_BY_DRY_RUN_GUARD"  # dry run: the page tried to send it
UPLOAD_FAILED = "UPLOAD_FAILED"


class ExecutionError(Exception):
    """Expected failure; ``code`` is a ToolErrorCode value."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass
class ExecContext:
    db: Session
    settings: Settings
    profile: UserProfile
    browser: BrowserManager
    mode: BrowserExecutionMode
    actor: str = "agent"  # agent | user
    run: AgentRun | None = None
    tool: str = ""
    state_changes: list[Any] = field(default_factory=list)


# --- gates --------------------------------------------------------------------------------


def check_live(settings: Settings) -> None:
    """Raise unless LIVE external actions are allowed. Runs before any live browser code."""
    block = live_execution_block(settings)
    if block is not None:
        raise ExecutionError(*block)
    if settings.browser_headless:
        raise ExecutionError(
            "VISIBLE_BROWSER_REQUIRED",
            "Live sessions may need you (CAPTCHA, OTP, login): set BROWSER_HEADLESS=false.",
        )


def check_daily_limit(
    ctx: ExecContext, session: BrowserSession | None, job: Job, app: Application
) -> None:
    """Raise when a live application for this job would go over today's limits."""
    reason = limit_block(ctx.db, ctx.settings.automation_path, ctx.settings.timezone, job)
    if reason is None:
        return
    _log(
        ctx,
        session,
        f"Refused: {reason}",
        ExternalActionResult.REFUSED,
        application_id=app.id,
        domain=domain_of(job.application_url or job.url),
    )
    ctx.db.commit()
    raise ExecutionError("DAILY_LIMIT_REACHED", reason)


# --- helpers ------------------------------------------------------------------------------


def banner(mode: BrowserExecutionMode) -> str:
    return DRY_RUN_BANNER if mode is BrowserExecutionMode.DRY_RUN else LIVE_BANNER


def _norm(text: str | None) -> str:
    return " ".join((text or "").lower().split())


def _log(
    ctx: ExecContext,
    session: BrowserSession | None,
    summary: str,
    result: ExternalActionResult,
    *,
    application_id: uuid.UUID | None = None,
    domain: str | None = None,
    error: str | None = None,
) -> None:
    ctx.db.add(
        ExternalActionLog(
            run_id=ctx.run.id if ctx.run else None,
            application_id=session.application_id if session else application_id,
            session_id=session.id if session else None,
            tool=ctx.tool or "execution",
            mode=(session.mode.value if session else ctx.mode.value),
            actor=ctx.actor,
            target_domain=session.target_domain if session else domain,
            summary=summary[:2000],
            result=result,
            error=error[:2000] if error else None,
        )
    )


def _event(
    ctx: ExecContext,
    job: Job,
    app: Application,
    event_type: str,
    reason: str,
    evidence: dict[str, Any] | None = None,
    previous: str | None = None,
    new: str | None = None,
) -> None:
    ctx.db.add(
        ApplicationEvent(
            job_id=job.id,
            application_id=app.id,
            event_type=event_type,
            source=EventSource.USER if ctx.actor == "user" else EventSource.BROWSER,
            previous_status=previous,
            new_status=new,
            reason=reason[:2000],
            evidence=evidence,
        )
    )


def _move(ctx: ExecContext, job: Job, app: Application, target: S, reason: str) -> None:
    previous = job.pipeline_state
    if previous is target:
        return
    assert_transition(previous, target)
    job.pipeline_state = target
    _event(ctx, job, app, "execution_state_changed", reason, None, previous.value, target.value)
    if ctx.state_changes is not None:
        from agent.tools.base import StateChange

        ctx.state_changes.append(
            StateChange("job", str(job.id), previous.value, target.value, reason)
        )


def _session(ctx: ExecContext, session_id: uuid.UUID) -> tuple[BrowserSession, Job, Application]:
    session = ctx.db.get(BrowserSession, session_id)
    if session is None:
        raise ExecutionError("NOT_FOUND", f"No browser session {session_id}")
    job, app = ctx.db.get(Job, session.job_id), ctx.db.get(Application, session.application_id)
    assert job is not None and app is not None
    if session.mode is not ctx.mode:
        raise ExecutionError(
            "MODE_MISMATCH",
            f"This session is {session.mode.value} but the request is {ctx.mode.value}. PIE "
            "never switches a session between dry run and live; close it and start a new one.",
        )
    if session.mode is BrowserExecutionMode.LIVE:
        check_live(ctx.settings)
    return session, job, app


def _require_active(session: BrowserSession, *states: B) -> None:
    allowed = set(states) or ACTIVE
    if session.state not in allowed:
        raise ExecutionError(
            "PRECONDITION_FAILED",
            f"Browser session is {session.state.value}; this step needs "
            + ", ".join(sorted(s.value for s in allowed)),
        )


FAILURE_SCREENSHOT_TIMEOUT = 20.0  # seconds: a crashed page must not hold up the cleanup


def _shoot(
    ctx: ExecContext, session: BrowserSession, label: str, timeout: float | None = None
) -> None:
    """Best effort: a page that cannot be photographed is recorded, never an error."""
    root = ctx.settings.application_artifacts_dir
    n = len(session.screenshots or []) + 1
    rel = Path(str(session.application_id)) / str(session.id) / f"{n:02d}_{label}.png"
    try:
        if timeout is None:
            ctx.browser.screenshot(str(session.id), root / rel)
        else:
            ctx.browser.screenshot(str(session.id), root / rel, timeout=timeout)
    except Exception as exc:  # crashed or hung page, disk full...
        error = f"screenshot failed: {type(exc).__name__}: {exc}"[:300]
        session.errors = [*(session.errors or []), {"step": label, "error": error}]
        return
    session.screenshots = [
        *(session.screenshots or []),
        {"label": label, "file": rel.as_posix(), "taken_at": utcnow().isoformat()},
    ]


def _record_page(session: BrowserSession, snap: Snapshot) -> None:
    session.current_url = snap.url[:2048]
    session.page_title = snap.title[:512]
    if snap.blocked_requests:
        session.blocked_requests = snap.blocked_requests[-50:]


def _check_domain(session: BrowserSession, snap: Snapshot) -> None:
    allowed = {session.target_domain, domain_of(session.target_url)}
    if snap.domain not in allowed:
        raise ExecutionError(
            "UNEXPECTED_PAGE",
            f"The browser is on {snap.domain}, not the application site "
            f"({', '.join(sorted(allowed))}). Nothing further was done.",
        )


def _block(
    ctx: ExecContext,
    session: BrowserSession,
    job: Job,
    app: Application,
    message: str,
    evidence: dict[str, Any],
) -> None:
    """Pause for the user: session WAITING_FOR_USER, job BLOCKED_NEEDS_USER. The browser page is
    kept open (while the backend runs) so the user can act in it."""
    session.state = B.WAITING_FOR_USER
    session.user_action_required = message
    session.current_step = "Waiting for you"
    app.requires_user_input = True
    app.user_action_required = message
    app.next_action = "Resolve the blocker, then resume the browser session"
    if job.pipeline_state in (S.APPLICATION_IN_PROGRESS, S.READY_FOR_SUBMISSION):
        _move(ctx, job, app, S.BLOCKED_NEEDS_USER, message)
    _event(ctx, job, app, "execution_blocked", message, evidence)
    _log(ctx, session, f"Paused for the user: {message}", ExternalActionResult.BLOCKED)


def _challenges(
    ctx: ExecContext, session: BrowserSession, job: Job, app: Application, snap: Snapshot
) -> list[dict[str, str]] | None:
    found = [c.as_dict() for c in detect_challenges(snap.signals, snap.http_status)]
    if not found:
        session.challenges = []
        return None
    session.challenges = found
    _shoot(ctx, session, "challenge")
    kinds = ", ".join(c["type"] for c in found)
    action = " ".join(dict.fromkeys(c["user_action"] for c in found))
    if getattr(ctx.browser, "headless", True):
        action += (
            " The browser is headless, so you cannot act in it: set BROWSER_HEADLESS=false, "
            "restart the backend and resume - the page will open in a visible window."
        )
    _block(
        ctx,
        session,
        job,
        app,
        f"{kinds} detected on {snap.domain}. {action}",
        {"challenges": found, "domain": snap.domain},
    )
    return found


def _validated_resume(ctx: ExecContext, app: Application) -> ResumeVersion | None:
    if app.resume_version_id is None:
        return None
    v = ctx.db.get(ResumeVersion, app.resume_version_id)
    if v is None or v.validation_status is not ResumeValidationStatus.VALIDATED:
        return None
    return v


def _resume_pdf(ctx: ExecContext, version: ResumeVersion | None) -> Path | None:
    if version is None:
        return None
    path = Path(version.file_path)
    if path.suffix.lower() != ".pdf":
        path = path.with_suffix(".pdf")
    tailored = (ctx.settings.resumes_dir / "tailored").resolve()
    if not path.is_file() or not path.resolve().is_relative_to(tailored):
        return None
    return path


def _bank(ctx: ExecContext, app: Application) -> AnswerBank:
    stored = ctx.db.scalars(
        select(ApplicationAnswer).where(
            (ApplicationAnswer.application_id == app.id)
            | (ApplicationAnswer.application_id.is_(None))
        )
    ).all()
    return AnswerBank(ctx.profile, list(stored), app.id)


def _norm_title(text: str | None) -> str:
    return " ".join((text or "").lower().split())


def find_duplicate(db: Session, job: Job, app: Application) -> str | None:
    """Has this job (or the same posting) already been applied to / attempted?"""
    if job.pipeline_state is S.SUBMITTED or app.applied_at is not None:
        return "This application is already submitted"
    attempts = db.scalars(
        select(SubmissionAttempt).where(SubmissionAttempt.application_id == app.id)
    ).all()
    if any(a.outcome in BLOCKING_OUTCOMES for a in attempts):
        return "A submission attempt already exists for this application"
    others = list(
        db.scalars(select(Job).where(Job.id != job.id, Job.pipeline_state == S.SUBMITTED)).all()
    )
    others += [  # jobs with an unresolved/confirmed attempt but not (yet) SUBMITTED
        j
        for j in db.scalars(
            select(Job)
            .join(Application, Application.job_id == Job.id)
            .join(SubmissionAttempt, SubmissionAttempt.application_id == Application.id)
            .where(Job.id != job.id, SubmissionAttempt.outcome.in_(BLOCKING_OUTCOMES))
        ).all()
    ]
    for other in others:
        if job.canonical_url and other.canonical_url == job.canonical_url:
            return f"Already applied to the same posting URL (job {other.id})"
        if other.url == job.url or (
            job.application_url and other.application_url == job.application_url
        ):
            return f"Already applied to the same application URL (job {other.id})"
        if other.source == job.source and other.source_job_id == job.source_job_id:
            return f"Already applied to external job id {job.source}:{job.source_job_id}"
        if (
            job.company_id is not None
            and other.company_id == job.company_id
            and _norm_title(other.title) == _norm_title(job.title)
        ):
            return f"Already applied to '{job.title}' at the same company (job {other.id})"
    return None


def next_generation(db: Session, app: Application) -> int:
    """A new submission key only after the user confirmed every earlier attempt was NOT sent
    (USER_NOT_SUBMITTED); any other earlier attempt keeps the same key, so the database
    itself refuses a second submission."""
    return (
        db.scalar(
            select(func.count())
            .select_from(SubmissionAttempt)
            .where(
                SubmissionAttempt.application_id == app.id,
                SubmissionAttempt.outcome == SubmissionOutcome.USER_NOT_SUBMITTED,
            )
        )
        or 0
    )


def submission_key(app: Application, job: Job, generation: int) -> str:
    target = job.canonical_url or job.application_url or job.url
    raw = f"{app.id}|{target}|submit|{generation}"
    return hashlib.sha256(raw.encode()).hexdigest()


def result(session: BrowserSession, job: Job, **extra: Any) -> dict[str, Any]:
    """Structured tool output. Field values beyond the user's own answers are never included."""
    return {
        "session_id": str(session.id),
        "application_id": str(session.application_id),
        "mode": session.mode.value,
        "banner": banner(session.mode),
        "state": session.state.value,
        "job_status": job.pipeline_state.value,
        "current_step": session.current_step,
        "current_url": session.current_url,
        "target_domain": session.target_domain,
        "challenges": session.challenges or [],
        "missing_answers": session.missing_answers or [],
        "blocked_needs_user": session.state is B.WAITING_FOR_USER,
        "user_action": session.user_action_required,
        **extra,
    }


# --- form planning ------------------------------------------------------------------------


def _plan_page(
    ctx: ExecContext, session: BrowserSession, job: Job, app: Application, snap: Snapshot
) -> None:
    """Classify every field on the page and store the plan (no browser interaction)."""
    version = _validated_resume(ctx, app)
    plans = plan_fields(
        snap.fields, _bank(ctx, app), job.location, _resume_pdf(ctx, version) is not None
    )
    session.detected_fields = [
        {
            **p.public(),
            "has_value": f.get("has_value", False),
            "options": option_labels(f),  # the page's own option texts (not the answer)
            "error": f.get("error") or None,
        }
        for p, f in zip(plans, snap.fields, strict=True)
    ]
    session.missing_answers = [
        {
            "field_id": p.field_id,
            "question": p.label,
            "question_key": p.key,
            "category": p.category,
            "reason": p.reason,
            "answer_status": p.answer_status,
            "options": option_labels(f),  # the page's own choices, shown to the user
        }
        for p, f in zip(plans, snap.fields, strict=True)
        if p.action == "block"
    ]
    session.skipped_fields = [
        {"field_id": p.field_id, "label": p.label, "category": p.category, "reason": p.reason}
        for p in plans
        if p.action == "skip"
    ]


def _plan_for(session: BrowserSession, field_id: str) -> dict[str, Any]:
    for f in session.detected_fields or []:
        if f["field_id"] == field_id:
            return f
    raise ExecutionError("NOT_FOUND", f"Field {field_id} was not detected; inspect the form first")


# --- filling one field ----------------------------------------------------------------------


@dataclass
class Fill:
    status: str  # filled | already | refused | stop
    record: dict[str, Any] | None = None
    reason: str = ""


def _record(plan: dict[str, Any], answer: Answer, method: str) -> dict[str, Any]:
    """What PIE keeps about a filled field: which answer went in, never the answer itself."""
    return {
        "field_id": plan["field_id"],
        "label": plan["label"],
        "key": plan["key"],
        "source": answer.source,
        "method": method,
    }


def _fill_dropdown(
    ctx: ExecContext, session: BrowserSession, plan: dict[str, Any], answer: Answer
) -> Fill:
    """Custom dropdown: open, search, select the ONE clearly matching option, verify it shows."""
    sid, fid = str(session.id), plan["field_id"]
    target = answer.value or ""
    if plan.get("preselected"):
        return Fill("already", _record(plan, answer, "dropdown (already set)"))
    listing = ctx.browser.combobox_options(sid, fid, None)
    if listing["selected"] and choose_option([listing["selected"]], target)[0] is not None:
        ctx.browser.combobox_close(sid, fid)
        return Fill("already", _record(plan, answer, "dropdown (already set)"))
    options: list[str] = listing["options"]
    index, why = choose_option(options, target) if options else (None, "no options listed")
    if index is None:
        # Searchable dropdowns may list nothing (or a subset) until you type.
        polarity = re.match(r"^(yes|no)\b", _norm(target))
        term = polarity.group(1).capitalize() if polarity else target
        listing = ctx.browser.combobox_options(sid, fid, term)
        options = listing["options"]
        index, why = choose_option(options, target) if options else (None, "no matching option")
    if index is None:
        ctx.browser.combobox_close(sid, fid)
        return Fill("stop", None, f"answer '{plan['key']}': {why}")
    shown = ctx.browser.combobox_select(sid, fid, index, options[index])
    if _norm(shown) != _norm(options[index]):
        ctx.browser.combobox_close(sid, fid)
        return Fill("refused", None, "the dropdown does not show the chosen option after selecting")
    return Fill("filled", _record(plan, answer, "dropdown"))


def _fill_one(
    ctx: ExecContext, session: BrowserSession, plan: dict[str, Any], bank: AnswerBank
) -> Fill:
    if plan.get("category") != VERIFIED_AUTOFILL or plan.get("action") != "fill":
        return Fill("refused", None, f"{plan.get('category')}: {plan.get('reason')}")
    answer = bank.get(plan["key"])
    if not answer.verified:  # re-checked at fill time: the bank may have changed
        return Fill("refused", None, f"answer '{plan['key']}' is {answer.status.value}")
    try:
        if plan["kind"] == "combobox":
            return _fill_dropdown(ctx, session, plan, answer)
        # The value (and which option) is worked out again here from the answer bank: the
        # stored plan never contains the answer.
        target = fill_target(plan["kind"], plan.get("options") or [], answer.value or "")
        if isinstance(target, str):
            return Fill("refused", None, f"answer '{plan['key']}': {target}")
        value, option, _ = target
        if plan["kind"] in ("select", "radio") and plan.get("preselected"):
            return Fill("already", _record(plan, answer, "already selected"))
        ok = ctx.browser.fill(str(session.id), plan["field_id"], plan["kind"], value, option)
    except BrowserError as exc:
        return Fill("refused", None, f"browser: {exc}")
    if not ok:
        return Fill("refused", None, "the page did not keep the value")
    return Fill("filled", _record(plan, answer, "direct"))


# --- uploads ---------------------------------------------------------------------------------


def _upload(
    ctx: ExecContext, session: BrowserSession, version: ResumeVersion, path: Path, field_id: str
) -> dict[str, Any]:
    dry = session.mode is BrowserExecutionMode.DRY_RUN
    record: dict[str, Any] = {
        "resume_version_id": str(version.id),
        "version_number": version.version_number,
        "file_name": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "field_id": field_id,
    }
    try:
        outcome = ctx.browser.upload(str(session.id), field_id, path)
        snap = ctx.browser.snapshot(str(session.id))
    except BrowserError as exc:
        return {**record, "status": UPLOAD_FAILED, "detail": str(exc)[:300]}
    field_state = next((f for f in snap.fields if f["id"] == field_id), None)
    error = (field_state or {}).get("error") or ""
    if path.name not in outcome.names or error:
        detail = error or "the page did not keep the file"
        return {**record, "status": UPLOAD_FAILED, "detail": detail[:300]}
    if dry and outcome.blocked_requests:
        return {
            **record,
            "status": UPLOAD_BLOCKED,
            "detail": f"The page tried to send the file ({outcome.blocked_requests} request(s)); "
            "the dry-run guard blocked it. The employer did NOT receive it.",
        }
    if dry:
        return {
            **record,
            "status": UPLOAD_UI_SUCCESS,
            "detail": "The file input holds the resume; nothing was sent (dry run).",
        }
    return {**record, "status": UPLOAD_SUCCESS, "detail": "The file input holds the resume."}


# --- recovery ------------------------------------------------------------------------------


def _ensure_page(ctx: ExecContext, session: BrowserSession, job: Job, app: Application) -> None:
    """Crash / backend-restart recovery. If the session's page no longer exists, reload the
    application page and rebuild the recorded form state: the same VERIFIED answers are
    entered again and verified, and the tailored resume is set again. Nothing is assumed
    about the old page."""
    if ctx.browser.is_alive(str(session.id)):
        return
    previous = list(session.filled_fields or [])
    had_upload = bool(session.uploaded_resume)
    snap = ctx.browser.open(
        str(session.id), session.target_url, dry_run=session.mode is BrowserExecutionMode.DRY_RUN
    )
    _record_page(session, snap)
    session.filled_fields, session.uploaded_resume = [], None
    session.state = B.OPEN
    if job.pipeline_state is S.READY_FOR_SUBMISSION:
        _move(ctx, job, app, S.APPLICATION_IN_PROGRESS, "Browser recovered; re-validating form")
    restored = 0
    uploaded = ""
    if not _challenges(ctx, session, job, app, snap) and snap.fields and session.detected_fields:
        _plan_page(ctx, session, job, app, snap)
        session.state = B.FORM_DETECTED
        bank = _bank(ctx, app)
        by_key = {f["key"]: f for f in session.detected_fields or [] if f["action"] == "fill"}
        refilled = []
        for prev in previous:
            plan = by_key.get(prev["key"])
            if plan is None:
                continue
            outcome = _fill_one(ctx, session, plan, bank)
            if outcome.status in ("filled", "already") and outcome.record:
                refilled.append(outcome.record)
        session.filled_fields = refilled
        restored = len(refilled)
        version = _validated_resume(ctx, app)
        path = _resume_pdf(ctx, version)
        target = next((f for f in session.detected_fields or [] if f["key"] == "resume"), None)
        if had_upload and version is not None and path is not None and target is not None:
            session.uploaded_resume = _upload(ctx, session, version, path, target["field_id"])
            uploaded = f"; resume {session.uploaded_resume['status']}"
        if refilled or session.uploaded_resume:
            session.state = B.FILLING
    message = (
        "Browser page was lost (crash or backend restart). Reloaded the application page and "
        f"restored {restored}/{len(previous)} previously filled fields{uploaded}."
    )
    session.errors = [*(session.errors or []), {"step": "recovery", "error": message}]
    _log(ctx, session, message, ExternalActionResult.SUCCESS)
    ctx.db.commit()  # the recovery is a fact, even if the calling step then refuses


# --- open ---------------------------------------------------------------------------------


def open_application(ctx: ExecContext, job_id: uuid.UUID) -> dict[str, Any]:
    if ctx.mode is BrowserExecutionMode.LIVE:
        check_live(ctx.settings)  # before anything else, including any browser code
    job = ctx.db.get(Job, job_id)
    if job is None:
        raise ExecutionError("NOT_FOUND", f"No job {job_id}")
    app = ctx.db.scalar(select(Application).where(Application.job_id == job.id))
    if app is None:
        raise ExecutionError(
            "PRECONDITION_FAILED", "No application draft: create_application_draft first"
        )
    existing = ctx.db.scalars(
        select(BrowserSession)
        .where(BrowserSession.application_id == app.id, BrowserSession.state.in_(ACTIVE))
        .order_by(BrowserSession.created_at.desc())
    ).first()
    if existing is not None and existing.mode is not ctx.mode:
        raise ExecutionError(
            "MODE_MISMATCH",
            f"An active {existing.mode.value} session exists. Close it first; PIE never "
            "upgrades a dry run to live (or the reverse).",
            {"session_id": str(existing.id)},
        )
    duplicate = find_duplicate(ctx.db, job, app)
    if duplicate:
        _log(
            ctx,
            None,
            f"Refused to open: {duplicate}",
            ExternalActionResult.REFUSED,
            application_id=app.id,
            domain=domain_of(job.application_url or job.url),
        )
        ctx.db.commit()
        raise ExecutionError("DUPLICATE_APPLICATION", duplicate)
    if ctx.mode is BrowserExecutionMode.LIVE:
        check_daily_limit(ctx, existing, job, app)

    resuming = existing is not None and existing.state is B.WAITING_FOR_USER
    state = job.pipeline_state
    if existing is not None and not resuming:
        if state not in (S.APPLICATION_IN_PROGRESS, S.READY_FOR_SUBMISSION):
            raise ExecutionError("PRECONDITION_FAILED", f"Job is {state.value}")
        try:
            _ensure_page(ctx, existing, job, app)
        except (BrowserError, ExecutionError) as exc:
            return _fail(ctx, existing, job, app, f"Could not restore the application page: {exc}")
        ctx.db.commit()
        return result(existing, job, reused=True, note="Session already open")
    if resuming:
        if state is not S.BLOCKED_NEEDS_USER:
            raise ExecutionError("PRECONDITION_FAILED", f"Job is {state.value}")
    elif state is not S.READY_TO_APPLY:
        raise ExecutionError(
            "PRECONDITION_FAILED",
            f"Job is {state.value}: browser execution starts only from READY_TO_APPLY "
            "(a draft, a validated tailored resume and every standard answer).",
        )
    readiness = evaluate_readiness(ctx.db, ctx.settings, ctx.profile, job, app)
    blockers = user_blockers(ctx.settings, ctx.profile, ctx.db)
    version = _validated_resume(ctx, app)
    if blockers or version is None or (not resuming and not readiness.ready):
        raise ExecutionError(
            "PRECONDITION_FAILED",
            "Not ready for browser execution",
            {"readiness": readiness.as_dict()},
        )
    if _resume_pdf(ctx, version) is None:
        raise ExecutionError(
            "PRECONDITION_FAILED", "The validated tailored resume has no PDF artifact to upload"
        )

    try:  # a browser that cannot start is reported before PIE creates a session or moves the job
        ctx.browser.start()
    except Exception as exc:
        raise ExecutionError("BROWSER_ERROR", f"The browser could not be started: {exc}") from exc

    url = job.application_url or job.url
    if resuming:
        assert existing is not None
        try:
            return _resume_session(ctx, existing, job, app)
        except (BrowserError, ExecutionError) as exc:
            return _fail(ctx, existing, job, app, f"Could not resume the application page: {exc}")
    session = BrowserSession(
        application_id=app.id,
        job_id=job.id,
        run_id=ctx.run.id if ctx.run else None,
        mode=ctx.mode,
        state=B.CREATED,
        target_url=url,
        target_domain=domain_of(url),
        detected_fields=[],
        filled_fields=[],
        skipped_fields=[],
        missing_answers=[],
        challenges=[],
        screenshots=[],
        errors=[],
        blocked_requests=[],
    )
    ctx.db.add(session)
    ctx.db.flush()
    _move(ctx, job, app, S.APPLICATION_IN_PROGRESS, f"Browser execution started ({ctx.mode.value})")
    session.state = B.NAVIGATING
    session.current_step = "Opening the application page"
    ctx.db.commit()

    try:
        snap = ctx.browser.open(
            str(session.id), url, dry_run=ctx.mode is BrowserExecutionMode.DRY_RUN
        )
    except Exception as exc:  # BrowserError, or anything else: never leave the job in progress
        return _fail(ctx, session, job, app, f"Could not open the application page: {exc}")
    session.target_domain = snap.domain or session.target_domain
    _record_page(session, snap)
    session.state = B.OPEN
    session.current_step = "Application page opened"
    _shoot(ctx, session, "opened")
    _log(ctx, session, f"Opened {snap.domain} ({ctx.mode.value})", ExternalActionResult.SUCCESS)
    if snap.http_status and snap.http_status >= 400 and snap.http_status not in (401, 403, 429):
        return _fail(
            ctx, session, job, app, f"The application page returned HTTP {snap.http_status}"
        )
    _challenges(ctx, session, job, app, snap)
    ctx.db.commit()
    return result(
        session,
        job,
        http_status=snap.http_status,
        title=snap.title,
        fields_on_page=len(snap.fields),
    )


def _resume_session(
    ctx: ExecContext, session: BrowserSession, job: Job, app: Application
) -> dict[str, Any]:
    """Continue a paused session. The page the user acted in is kept if it still exists (a
    solved CAPTCHA or a login stays solved); after a restart it is reloaded and rebuilt."""
    session.user_action_required = None
    session.missing_answers = []
    app.requires_user_input, app.user_action_required = False, None
    _move(ctx, job, app, S.APPLICATION_IN_PROGRESS, "Resuming the paused browser session")
    alive = ctx.browser.is_alive(str(session.id))
    if session.filled_fields:
        session.state = B.FILLING
    else:
        session.state = B.FORM_DETECTED if session.detected_fields else B.OPEN
    if not alive:
        session.state = B.OPEN
        _ensure_page(ctx, session, job, app)
    if session.state is B.WAITING_FOR_USER:  # the reloaded page still shows the challenge
        ctx.db.commit()
        return result(session, job, resumed=True)
    try:
        snap = ctx.browser.snapshot(str(session.id))
    except BrowserError as exc:
        raise ExecutionError("BROWSER_ERROR", str(exc)) from exc
    _record_page(session, snap)
    session.current_step = "Resumed"
    _challenges(ctx, session, job, app, snap)
    _log(ctx, session, "Resumed the paused session", ExternalActionResult.SUCCESS)
    ctx.db.commit()
    return result(session, job, resumed=True, page_kept=alive)


def _fail(
    ctx: ExecContext,
    session: BrowserSession,
    job: Job,
    app: Application,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A failure PIE cannot recover from on its own (REQUIREMENTS 12): take a screenshot, log
    the error with the step it happened at, mark the job FAILED and close the browser page.
    Nothing is retried automatically; the user retries failed jobs themselves."""
    stage = ctx.tool or "execution"
    # 1. The outcome first, so the job ends up FAILED whatever the browser does next.
    session.state = B.FAILED
    session.current_step = f"Failed at {stage}"
    session.errors = [*(session.errors or []), {"step": stage, "error": message[:500]}]
    app.last_error = message[:2000]
    app.next_action = "Failed: check the error and screenshot, then retry the job yourself"
    if job.pipeline_state in (S.APPLICATION_IN_PROGRESS, S.READY_FOR_SUBMISSION):
        _move(ctx, job, app, S.FAILED, message)
    _event(ctx, job, app, "execution_failed", message, {"stage": stage, **(details or {})})
    _log(ctx, session, f"Failed at {stage}: {message}", ExternalActionResult.FAILED, error=message)
    ctx.db.commit()
    # 2. Best effort: what the page looked like, then free the page and its browser context.
    #    Either may fail on a crashed or hung page; that is recorded, not raised.
    _shoot(ctx, session, "failed", timeout=FAILURE_SCREENSHOT_TIMEOUT)
    try:
        ctx.browser.close(str(session.id))
    except Exception as exc:
        error = f"closing the page failed: {type(exc).__name__}: {exc}"[:300]
        session.errors = [*(session.errors or []), {"step": "close", "error": error}]
    ctx.db.commit()
    return result(session, job, failed=True, error=message, failed_stage=stage)


def fail_session(
    ctx: ExecContext, session_id: uuid.UUID, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Fail an active session after an error (see ``_fail``). A session that already ended, or
    that is paused for the user, is left alone. Returns the failure result, or None."""
    ctx.db.rollback()  # drop half-done changes from the failed step; keep what was committed
    session = ctx.db.get(BrowserSession, session_id)
    if session is None or session.state not in ACTIVE or session.state is B.WAITING_FOR_USER:
        return None
    job, app = ctx.db.get(Job, session.job_id), ctx.db.get(Application, session.application_id)
    if job is None or app is None:
        return None
    return _fail(ctx, session, job, app, message, details)


# --- inspect ------------------------------------------------------------------------------


def inspect_form(
    ctx: ExecContext, session_id: uuid.UUID, *, click_apply: bool = False
) -> dict[str, Any]:
    session, job, app = _session(ctx, session_id)
    _require_active(session, B.OPEN, B.FORM_DETECTED, B.FILLING)
    _ensure_page(ctx, session, job, app)
    if session.state is B.WAITING_FOR_USER:
        return result(session, job)
    try:
        snap = ctx.browser.snapshot(str(session.id))
        if not snap.fields and click_apply and snap.apply_buttons:
            snap = ctx.browser.click_apply(str(session.id), snap.apply_buttons[0]["id"])
    except BrowserError as exc:
        raise ExecutionError("BROWSER_ERROR", str(exc)) from exc
    _record_page(session, snap)
    _check_domain(session, snap)
    if _challenges(ctx, session, job, app, snap):
        ctx.db.commit()
        return result(session, job)
    if not snap.fields:
        session.current_step = "No application form found"
        _log(
            ctx,
            session,
            "Inspected page: no form",
            ExternalActionResult.FAILED,
            error="missing form",
        )
        ctx.db.commit()
        raise ExecutionError(
            "PRECONDITION_FAILED",
            "No application form on this page"
            + (
                " (an Apply button exists: call again with click_apply=true)"
                if snap.apply_buttons
                else ""
            ),
            {"apply_buttons": [b["label"] for b in snap.apply_buttons]},
        )

    first_time = not session.detected_fields
    _plan_page(ctx, session, job, app, snap)
    session.state = B.FORM_DETECTED if session.state is B.OPEN else session.state
    session.current_step = f"Form detected: {len(session.detected_fields or [])} fields"
    if first_time:
        _shoot(ctx, session, "form_detected")
    _log(
        ctx,
        session,
        f"Inspected form: {len(session.detected_fields or [])} fields, "
        f"{len(session.missing_answers or [])} need the user",
        ExternalActionResult.SUCCESS,
    )
    if session.missing_answers:
        questions = "; ".join(f"{m['question']} [{m['category']}]" for m in session.missing_answers)
        _block(
            ctx,
            session,
            job,
            app,
            f"The form asks questions PIE may not answer: {questions}",
            {"missing_answers": session.missing_answers},
        )
    ctx.db.commit()
    return result(
        session,
        job,
        fields=session.detected_fields,
        missing_required_fields=list(session.missing_answers or []),
        skipped_fields=session.skipped_fields,
        submit_available=bool(snap.submit_buttons),
        verified_answer_keys=_bank(ctx, app).verified_keys(),
    )


# --- fill ---------------------------------------------------------------------------------


def fill_fields(
    ctx: ExecContext,
    session_id: uuid.UUID,
    assignments: list[tuple[str, str]] | None,
) -> dict[str, Any]:
    """Fill VERIFIED_AUTOFILL fields only. ``assignments`` = [(field_id, answer_key)] selects a
    subset of them (the key must be the one the mapping chose); None fills all of them.
    Claude never supplies values and cannot re-map a field to a different answer."""
    session, job, app = _session(ctx, session_id)
    _require_active(session, B.FORM_DETECTED, B.FILLING)
    _ensure_page(ctx, session, job, app)
    if session.state not in (B.FORM_DETECTED, B.FILLING):
        return result(session, job, filled=[], refused=[])
    bank = _bank(ctx, app)
    detected = {f["field_id"]: f for f in session.detected_fields or []}
    if assignments is None:
        targets = [
            (f["field_id"], f["key"])
            for f in detected.values()
            if f["action"] == "fill" and f["category"] == VERIFIED_AUTOFILL
        ]
    else:
        targets = assignments
    filled = {f["field_id"]: f for f in session.filled_fields or []}
    done: list[str] = []
    refused: list[dict[str, str]] = []
    stops: list[dict[str, Any]] = []
    for field_id, key in targets:
        plan = _plan_for(session, field_id)
        if key != plan["key"]:
            refused.append(
                {
                    "field_id": field_id,
                    "reason": f"'{plan['label']}' is mapped to {plan['key']!r}, not {key!r}; "
                    "fields cannot be re-mapped",
                }
            )
            continue
        if field_id in filled and filled[field_id]["key"] == key:
            continue  # idempotent: already filled with this answer
        outcome = _fill_one(ctx, session, plan, bank)
        if outcome.status in ("filled", "already") and outcome.record:
            filled[field_id] = outcome.record
            if outcome.status == "filled":
                done.append(field_id)
        elif outcome.status == "stop":
            stops.append({"plan": plan, "reason": outcome.reason})
        else:
            refused.append({"field_id": field_id, "reason": outcome.reason})
    session.filled_fields = list(filled.values())
    session.state = B.FILLING
    session.current_step = f"Filled {len(session.filled_fields)} fields"
    for stop in stops:  # a dropdown had no clear option for the verified answer
        plan = stop["plan"]
        entry = {
            "field_id": plan["field_id"],
            "question": plan["label"],
            "question_key": plan["key"],
            "category": AMBIGUOUS,
            "reason": stop["reason"],
            "answer_status": plan.get("answer_status"),
        }
        if plan["required"]:
            session.missing_answers = [*(session.missing_answers or []), entry]
        else:
            session.skipped_fields = [
                *(session.skipped_fields or []),
                {k: entry[k] for k in ("field_id", "category", "reason")}
                | {"label": plan["label"]},
            ]
    try:
        snap = ctx.browser.snapshot(str(session.id))
    except BrowserError as exc:
        raise ExecutionError("BROWSER_ERROR", str(exc)) from exc
    _record_page(session, snap)
    field_errors = [
        {"field_id": f["id"], "label": f.get("label"), "error": f.get("error") or "invalid value"}
        for f in snap.fields
        if f.get("invalid") or (f.get("error") and f["id"] in filled)
    ]
    if done:
        _shoot(ctx, session, "fields_completed")
    _log(
        ctx,
        session,
        f"Filled {len(done)} field(s); {len(refused)} refused; {len(stops)} without a clear option",
        ExternalActionResult.SUCCESS if not refused else ExternalActionResult.FAILED,
        error="; ".join(r["reason"] for r in refused)[:500] or None,
    )
    if not _challenges(ctx, session, job, app, snap) and session.missing_answers:
        questions = "; ".join(f"{m['question']} [{m['category']}]" for m in session.missing_answers)
        _block(
            ctx,
            session,
            job,
            app,
            f"No clear option for a verified answer - PIE will not guess: {questions}",
            {"missing_answers": session.missing_answers},
        )
    ctx.db.commit()
    return result(
        session,
        job,
        filled=done,
        refused=refused,
        field_errors=field_errors,
        filled_fields=[
            {k: f.get(k) for k in ("field_id", "label", "key", "method")}
            for f in session.filled_fields
        ],
    )


# --- upload -------------------------------------------------------------------------------


def upload_resume(ctx: ExecContext, session_id: uuid.UUID, field_id: str | None) -> dict[str, Any]:
    session, job, app = _session(ctx, session_id)
    _require_active(session, B.FORM_DETECTED, B.FILLING)
    _ensure_page(ctx, session, job, app)
    version = _validated_resume(ctx, app)
    path = _resume_pdf(ctx, version)
    if version is None or path is None:
        raise ExecutionError("PRECONDITION_FAILED", "No validated tailored resume PDF to upload")
    resume_fields = [f for f in session.detected_fields or [] if f["key"] == "resume"]
    target = _plan_for(session, field_id) if field_id else next(iter(resume_fields), None)
    if target is None or target["key"] != "resume":
        raise ExecutionError("NOT_FOUND", "No resume upload field was detected on this form")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    current = session.uploaded_resume or {}
    if current.get("sha256") == digest and current.get("status") != UPLOAD_FAILED:
        return result(session, job, uploaded=current, note="Already uploaded")
    record = _upload(ctx, session, version, path, target["field_id"])
    session.uploaded_resume = record
    status = record["status"]
    if status == UPLOAD_FAILED:
        _log(
            ctx,
            session,
            "Resume upload failed",
            ExternalActionResult.FAILED,
            error=record["detail"],
        )
        ctx.db.commit()
        raise ExecutionError(
            "BROWSER_ERROR", f"File upload failed: {record['detail']}", {"upload_status": status}
        )
    session.state = B.FILLING
    session.current_step = f"Resume v{version.version_number}: {status}"
    _log(
        ctx,
        session,
        f"Resume v{version.version_number} ({path.name}): {status} - {record['detail']}",
        ExternalActionResult.DRY_RUN
        if session.mode is BrowserExecutionMode.DRY_RUN
        else ExternalActionResult.SUCCESS,
    )
    ctx.db.commit()
    return result(session, job, uploaded=record)


# --- save / prepare -----------------------------------------------------------------------


def save_progress(ctx: ExecContext, session_id: uuid.UUID) -> dict[str, Any]:
    """Record the current state and a screenshot in PIE (nothing is clicked on the site)."""
    session, job, app = _session(ctx, session_id)
    if session.state in ACTIVE and ctx.browser.is_alive(str(session.id)):
        with contextlib.suppress(BrowserError):
            _record_page(session, ctx.browser.snapshot(str(session.id)))
        _shoot(ctx, session, "saved")
    session.updated_at = utcnow()
    ctx.db.commit()
    return result(
        session,
        job,
        filled_fields=[
            {k: f.get(k) for k in ("field_id", "label", "key", "method")}
            for f in session.filled_fields or []
        ],
        uploaded=session.uploaded_resume,
        note="Progress saved in PIE",
    )


def prepare_submission(ctx: ExecContext, session_id: uuid.UUID) -> dict[str, Any]:
    session, job, app = _session(ctx, session_id)
    if session.state is B.READY_FOR_SUBMISSION:
        return result(session, job, ready=True, note="Already prepared")
    _require_active(session, B.FILLING, B.FORM_DETECTED)
    _ensure_page(ctx, session, job, app)
    if session.state not in (B.FILLING, B.FORM_DETECTED):
        return result(session, job, ready=False)
    try:
        snap = ctx.browser.snapshot(str(session.id))
    except BrowserError as exc:
        raise ExecutionError("BROWSER_ERROR", str(exc)) from exc
    _record_page(session, snap)
    _check_domain(session, snap)
    if _challenges(ctx, session, job, app, snap):
        ctx.db.commit()
        return result(session, job, ready=False)
    problems: list[str] = []
    if session.missing_answers:
        problems.append("questions without a verified answer remain")
    for f in snap.fields:
        if f.get("required") and not f.get("has_value"):
            problems.append(f"required field empty: {f.get('label') or f.get('name')}")
        if f.get("invalid") or (f.get("error") and f.get("has_value")):
            problems.append(f"invalid: {f.get('label')}: {f.get('error') or 'invalid value'}")
    upload = session.uploaded_resume or {}
    needs_resume = any(f["key"] == "resume" for f in session.detected_fields or [])
    if needs_resume and upload.get("status") in (None, UPLOAD_FAILED):
        problems.append("the tailored resume is not in the form")
    if (
        session.mode is BrowserExecutionMode.LIVE
        and needs_resume
        and upload.get("status") not in (None, UPLOAD_SUCCESS)
    ):
        problems.append(f"resume upload status is {upload.get('status')}")
    if len(snap.submit_buttons) != 1:
        problems.append(f"expected exactly one submit button, found {len(snap.submit_buttons)}")
    if problems:
        session.current_step = "Pre-submission validation failed"
        _log(
            ctx,
            session,
            "Pre-submission validation failed",
            ExternalActionResult.FAILED,
            error="; ".join(problems)[:500],
        )
        ctx.db.commit()
        raise ExecutionError("VALIDATION_FAILED", "The form is not ready", {"problems": problems})

    session.state = B.READY_FOR_SUBMISSION
    session.prepared_at = utcnow()
    session.user_confirmed_at = session.user_confirmation = None  # confirmation is per-prepare
    dry = session.mode is BrowserExecutionMode.DRY_RUN
    session.current_step = (
        "Dry run complete: stopped before the final submit. Nothing has been submitted."
        if dry
        else "Ready for submission: waiting for your explicit confirmation"
    )
    _shoot(ctx, session, "pre_submission")
    _move(ctx, job, app, S.READY_FOR_SUBMISSION, session.current_step)
    _log(
        ctx,
        session,
        (
            "DRY RUN: prepared; final submit NOT performed"
            if dry
            else "Prepared for live submission"
        ),
        ExternalActionResult.DRY_RUN if dry else ExternalActionResult.SUCCESS,
    )
    if dry:
        _event(ctx, job, app, "dry_run_completed", DRY_RUN_BANNER, dry_run_report(session))
    ctx.db.commit()
    return result(
        session,
        job,
        ready=True,
        dry_run_complete=dry,
        submit_button=snap.submit_buttons[0]["label"],
        report=dry_run_report(session) if dry else None,
    )


def dry_run_report(session: BrowserSession) -> dict[str, Any]:
    upload = session.uploaded_resume or {}
    return {
        "banner": DRY_RUN_BANNER,
        "url": session.current_url,
        "detected_fields": len(session.detected_fields or []),
        "fields_filled": [f["label"] for f in session.filled_fields or []],
        "fields_skipped": [f["label"] for f in session.skipped_fields or []],
        "missing_answers": [m["question"] for m in session.missing_answers or []],
        "uploaded_resume": upload.get("file_name"),
        "upload_status": upload.get("status"),
        "upload_detail": upload.get("detail"),
        "screenshots": [s["file"] for s in session.screenshots or []],
        "blocked_requests": len(session.blocked_requests or []),
    }


# --- submit / verify ----------------------------------------------------------------------


def submit(ctx: ExecContext, session_id: uuid.UUID) -> dict[str, Any]:
    session = ctx.db.get(BrowserSession, session_id)
    if session is None:
        raise ExecutionError("NOT_FOUND", f"No browser session {session_id}")
    if session.mode is not BrowserExecutionMode.LIVE or ctx.mode is not BrowserExecutionMode.LIVE:
        _log(
            ctx,
            session,
            "Submit requested in DRY RUN: refused, nothing submitted",
            ExternalActionResult.DRY_RUN,
        )
        ctx.db.commit()
        raise ExecutionError(
            "DRY_RUN_ONLY", f"{DRY_RUN_BANNER} Dry runs stop before the final submit."
        )
    try:
        check_live(ctx.settings)  # DEVELOPMENT_MODE / live flag / visible browser, first
    except ExecutionError as exc:
        _log(ctx, session, f"Submit refused: {exc.message}", ExternalActionResult.REFUSED)
        ctx.db.commit()
        raise
    session, job, app = _session(ctx, session_id)
    # 1. database
    duplicate = find_duplicate(ctx.db, job, app)
    if duplicate:
        _log(ctx, session, f"Submit refused: {duplicate}", ExternalActionResult.REFUSED)
        ctx.db.commit()
        raise ExecutionError("DUPLICATE_APPLICATION", duplicate)
    check_daily_limit(ctx, session, job, app)  # again right before the click
    # 2. application state
    if job.pipeline_state is not S.READY_FOR_SUBMISSION:
        raise ExecutionError(
            "PRECONDITION_FAILED", f"Job is {job.pipeline_state.value}, not READY_FOR_SUBMISSION"
        )
    # 3. browser session state
    if session.state is not B.READY_FOR_SUBMISSION or not ctx.browser.is_alive(str(session.id)):
        raise ExecutionError(
            "PRECONDITION_FAILED",
            "The browser session is not prepared (or was lost); prepare the submission again",
        )
    # 4. existing evidence
    if session.submission_evidence:
        raise ExecutionError("DUPLICATE_APPLICATION", "Submission evidence already exists")
    # 5. the user's explicit confirmation of THIS prepared submission
    if session.user_confirmed_at is None or (
        session.prepared_at and session.user_confirmed_at < session.prepared_at
    ):
        raise ExecutionError(
            "USER_CONFIRMATION_REQUIRED",
            "The user has not confirmed this submission. Only the user can confirm it in the "
            "dashboard.",
        )
    if session.missing_answers:
        raise ExecutionError("PRECONDITION_FAILED", "Questions without a verified answer remain")
    try:
        snap = ctx.browser.snapshot(str(session.id))
    except BrowserError as exc:
        raise ExecutionError("BROWSER_ERROR", str(exc)) from exc
    _check_domain(session, snap)
    if _challenges(ctx, session, job, app, snap):
        ctx.db.commit()
        return result(session, job, submitted=False)
    if len(snap.submit_buttons) != 1 or any(
        f.get("required") and not f.get("has_value") for f in snap.fields
    ):
        raise ExecutionError("PRECONDITION_FAILED", "The form changed since it was prepared")

    attempt = SubmissionAttempt(
        idempotency_key=submission_key(app, job, next_generation(ctx.db, app)),
        application_id=app.id,
        session_id=session.id,
        outcome=SubmissionOutcome.PENDING,
    )
    ctx.db.add(attempt)
    try:
        ctx.db.commit()  # committed BEFORE the click: a crash can never lead to a re-submit
    except IntegrityError as exc:
        ctx.db.rollback()
        raise ExecutionError(
            "DUPLICATE_APPLICATION", "A submission with this idempotency key already exists"
        ) from exc
    before_url = session.current_url
    try:
        after = ctx.browser.click_submit(str(session.id), snap.submit_buttons[0]["id"])
    except BrowserError as exc:
        message = (
            f"The submit click failed or the browser crashed ({exc}). Whether the "
            "application reached the employer is UNKNOWN - PIE will not retry. Check "
            "your email / the employer portal, then resolve it in the dashboard."
        )
        attempt.evidence = {"error": str(exc)[:500]}
        _block(ctx, session, job, app, message, {"attempt": str(attempt.id)})
        ctx.db.commit()
        return result(session, job, submitted=False, outcome="UNKNOWN")
    _record_page(session, after)
    form_left = bool(after.submit_buttons) and any(f.get("required") for f in after.fields)
    check = classify_submission(
        after.signals.get("text") or "", after.url, after.http_status, form_still_present=form_left
    )
    _shoot(ctx, session, "after_submit")
    evidence = {
        "outcome": check.outcome,
        "signals": check.signals,
        "application_id": check.application_id,
        "excerpt": check.excerpt,
        "url_before": before_url,
        "url_after": after.url,
        "http_status": after.http_status,
        "screenshot": (session.screenshots or [{}])[-1].get("file"),
        "checked_at": utcnow().isoformat(),
    }
    attempt.evidence = evidence
    if check.outcome == "CONFIRMED":
        attempt.outcome = SubmissionOutcome.CONFIRMED
        _mark_submitted(ctx, session, job, app, evidence, "Employer confirmed the submission")
        ctx.db.commit()
        return result(session, job, submitted=True, evidence=evidence)
    if check.outcome == "FAILED":
        attempt.outcome = SubmissionOutcome.FAILED
        message = (
            "The employer showed an error after submit: "
            f"{'; '.join(check.signals)}. PIE will not retry the submission. Check the "
            "page and resolve it in the dashboard."
        )
    else:
        attempt.outcome = SubmissionOutcome.AMBIGUOUS
        message = (
            "Submission could NOT be confirmed (no confirmation message or application "
            "id). It may or may not have reached the employer - PIE will not retry. "
            "Check your email / the portal, then resolve it in the dashboard."
        )
    _block(ctx, session, job, app, message, {"evidence": evidence})
    ctx.db.commit()
    return result(session, job, submitted=False, outcome=attempt.outcome.value, evidence=evidence)


def _mark_submitted(
    ctx: ExecContext,
    session: BrowserSession,
    job: Job,
    app: Application,
    evidence: dict[str, Any],
    reason: str,
) -> None:
    assert session.mode is BrowserExecutionMode.LIVE  # a dry run can never get here
    session.state = B.SUBMITTED
    session.submission_evidence = evidence
    session.current_step = "Submitted (confirmed)"
    session.user_action_required = None
    app.status = ApplicationStatus.APPLIED
    app.applied_at = utcnow()
    app.requires_user_input, app.user_action_required = False, None
    app.next_action = None
    _move(ctx, job, app, S.SUBMITTED, reason)
    _event(ctx, job, app, "application_submitted", reason, evidence)
    _log(ctx, session, f"SUBMITTED: {reason}", ExternalActionResult.SUCCESS)
    if ctx.run is not None:
        ctx.run.applications_submitted = (ctx.run.applications_submitted or 0) + 1


def verify_submission(ctx: ExecContext, session_id: uuid.UUID) -> dict[str, Any]:
    session = ctx.db.get(BrowserSession, session_id)
    if session is None:
        raise ExecutionError("NOT_FOUND", f"No browser session {session_id}")
    if session.mode is not BrowserExecutionMode.LIVE:
        job = ctx.db.get(Job, session.job_id)
        assert job is not None
        return result(session, job, submitted=False, note=f"{DRY_RUN_BANNER} No submission exists.")
    session, job, app = _session(ctx, session_id)
    attempt = ctx.db.scalars(
        select(SubmissionAttempt)
        .where(SubmissionAttempt.session_id == session.id)
        .order_by(SubmissionAttempt.attempted_at.desc())
    ).first()
    if attempt is None:
        return result(session, job, submitted=False, note="No submission was attempted")
    if attempt.outcome in (SubmissionOutcome.CONFIRMED, SubmissionOutcome.USER_CONFIRMED):
        return result(session, job, submitted=True, evidence=attempt.evidence)
    if not ctx.browser.is_alive(str(session.id)):
        return result(
            session,
            job,
            submitted=False,
            outcome=attempt.outcome.value,
            note="Browser page is gone; only the user can resolve this now",
        )
    snap = ctx.browser.snapshot(str(session.id))
    form_left = bool(snap.submit_buttons) and any(f.get("required") for f in snap.fields)
    check = classify_submission(
        snap.signals.get("text") or "", snap.url, snap.http_status, form_still_present=form_left
    )
    _log(ctx, session, f"Verified submission page: {check.outcome}", ExternalActionResult.SUCCESS)
    if check.outcome == "CONFIRMED":
        evidence = {
            **(attempt.evidence or {}),
            "verified": check.signals,
            "application_id": check.application_id,
            "verified_at": utcnow().isoformat(),
        }
        attempt.outcome, attempt.evidence = SubmissionOutcome.CONFIRMED, evidence
        _mark_submitted(ctx, session, job, app, evidence, "Confirmation found on re-check")
        ctx.db.commit()
        return result(session, job, submitted=True, evidence=evidence)
    ctx.db.commit()
    return result(
        session, job, submitted=False, outcome=attempt.outcome.value, signals=check.signals
    )


# --- close --------------------------------------------------------------------------------


def close_session(ctx: ExecContext, session_id: uuid.UUID) -> dict[str, Any]:
    session = ctx.db.get(BrowserSession, session_id)
    if session is None:
        raise ExecutionError("NOT_FOUND", f"No browser session {session_id}")
    job, app = ctx.db.get(Job, session.job_id), ctx.db.get(Application, session.application_id)
    assert job is not None and app is not None
    with contextlib.suppress(BrowserError):
        ctx.browser.close(str(session.id))
    if session.state in (B.CLOSED, B.SUBMITTED, B.FAILED):
        session.closed_at = session.closed_at or utcnow()
        ctx.db.commit()
        return result(session, job, note="Session already finished")
    unresolved = ctx.db.scalar(
        select(func.count())
        .select_from(SubmissionAttempt)
        .where(
            SubmissionAttempt.session_id == session.id,
            SubmissionAttempt.outcome.in_(
                [SubmissionOutcome.PENDING, SubmissionOutcome.AMBIGUOUS, SubmissionOutcome.FAILED]
            ),
        )
    )
    dry_done = session.mode is BrowserExecutionMode.DRY_RUN and session.prepared_at is not None
    session.state = B.CLOSED
    session.closed_at = utcnow()
    session.current_step = (
        "Dry run complete: stopped before the final submit. Nothing has been submitted."
        if dry_done
        else "Closed without submitting"
    )
    if not unresolved and job.pipeline_state in (
        S.APPLICATION_IN_PROGRESS,
        S.READY_FOR_SUBMISSION,
        S.BLOCKED_NEEDS_USER,
    ):
        _move(ctx, job, app, S.READY_TO_APPLY, "Browser session closed without submitting")
        app.requires_user_input, app.user_action_required = False, None
    _log(ctx, session, "Closed the browser session", ExternalActionResult.SUCCESS)
    ctx.db.commit()
    return result(
        session,
        job,
        dry_run_complete=dry_done,
        report=dry_run_report(session) if dry_done else None,
    )


# --- user-only operations (dashboard; never exposed as agent tools) ----------------------


def provide_answers(
    db: Session, app: Application, answers: list[dict[str, Any]]
) -> list[ApplicationAnswer]:
    """The user answers questions the profile did not cover. Stored as VERIFIED (source user)."""
    rows = []
    for item in answers:
        text = (item.get("answer") or "").strip()
        if not text:
            continue
        row = ApplicationAnswer(
            application_id=None if item.get("remember") else app.id,
            question_key=item["question_key"],
            question=(item.get("question") or item["question_key"])[:2000],
            answer=text[:2000],
            status=AnswerStatus.VERIFIED,
            source=f"answered by the user in the dashboard on {utcnow().date().isoformat()}",
        )
        db.add(row)
        rows.append(row)
    db.add(
        ApplicationEvent(
            job_id=app.job_id,
            application_id=app.id,
            event_type="user_answered_questions",
            source=EventSource.USER,
            reason=f"{len(rows)} answer(s) provided",
            evidence={"question_keys": [r.question_key for r in rows]},
        )
    )
    db.commit()
    return rows


def confirm_live_submission(
    db: Session, settings: Settings, session_id: uuid.UUID, statement: str
) -> BrowserSession:
    """The user's explicit confirmation for one prepared LIVE submission."""
    session = db.get(BrowserSession, session_id)
    if session is None:
        raise ExecutionError("NOT_FOUND", f"No browser session {session_id}")
    if session.mode is not BrowserExecutionMode.LIVE:
        raise ExecutionError("DRY_RUN_ONLY", f"{DRY_RUN_BANNER} Dry runs cannot be confirmed.")
    check_live(settings)
    if session.state is not B.READY_FOR_SUBMISSION:
        raise ExecutionError("PRECONDITION_FAILED", "Only a prepared submission can be confirmed")
    session.user_confirmed_at = utcnow()
    session.user_confirmation = statement[:1000]
    job = db.get(Job, session.job_id)
    assert job is not None
    db.add(
        ApplicationEvent(
            job_id=job.id,
            application_id=session.application_id,
            event_type="live_submission_confirmed_by_user",
            source=EventSource.USER,
            reason=statement[:1000],
        )
    )
    db.commit()
    return session


def resolve_submission(
    ctx: ExecContext, session_id: uuid.UUID, submitted: bool, note: str
) -> dict[str, Any]:
    """The USER settles an unconfirmed live submission (after checking email / the portal)."""
    session = ctx.db.get(BrowserSession, session_id)
    if session is None:
        raise ExecutionError("NOT_FOUND", f"No browser session {session_id}")
    job, app = ctx.db.get(Job, session.job_id), ctx.db.get(Application, session.application_id)
    assert job is not None and app is not None
    attempt = ctx.db.scalars(
        select(SubmissionAttempt)
        .where(SubmissionAttempt.session_id == session.id)
        .order_by(SubmissionAttempt.attempted_at.desc())
    ).first()
    if (
        session.mode is not BrowserExecutionMode.LIVE
        or attempt is None
        or attempt.outcome
        not in (SubmissionOutcome.PENDING, SubmissionOutcome.AMBIGUOUS, SubmissionOutcome.FAILED)
    ):
        raise ExecutionError("PRECONDITION_FAILED", "There is no unresolved live submission")
    evidence = {
        **(attempt.evidence or {}),
        "user_resolution": "submitted" if submitted else "not_submitted",
        "user_note": note[:1000],
        "resolved_at": utcnow().isoformat(),
    }
    attempt.evidence = evidence
    if submitted:
        attempt.outcome = SubmissionOutcome.USER_CONFIRMED
        _mark_submitted(ctx, session, job, app, evidence, "The user confirmed the submission")
    else:
        attempt.outcome = SubmissionOutcome.USER_NOT_SUBMITTED
        session.state, session.closed_at = B.CLOSED, utcnow()
        session.current_step = "The user confirmed it was not submitted"
        app.requires_user_input, app.user_action_required = False, None
        if job.pipeline_state is S.BLOCKED_NEEDS_USER or job.pipeline_state is S.FAILED:
            _move(ctx, job, app, S.READY_TO_APPLY, "The user confirmed it was not submitted")
    ctx.db.commit()
    return result(session, job, submitted=submitted, evidence=evidence)


def run_execution(ctx: ExecContext, job_id: uuid.UUID) -> dict[str, Any]:
    """Deterministic execution (no Claude): open, inspect, fill verified answers, upload the
    tailored resume, validate - and STOP before the final submit.

    DRY_RUN: the session is then closed ("Nothing has been submitted").
    LIVE: the session stays READY_FOR_SUBMISSION until the user explicitly confirms and
    submits in the dashboard. This function never submits."""
    if ctx.mode is BrowserExecutionMode.LIVE:
        check_live(ctx.settings)
    steps: list[dict[str, Any]] = []
    ctx.tool = "open_application"
    out = open_application(ctx, job_id)  # refusals here happen before any session exists
    steps.append({"step": "open", "state": out["state"]})
    sid = uuid.UUID(out["session_id"])
    if out.get("failed") or out["blocked_needs_user"]:
        return {**out, "steps": steps}
    try:
        return _run_steps(ctx, sid, steps)
    except ExecutionError as exc:
        message, details = exc.message, {"code": exc.code, **exc.details}
    except BrowserError as exc:
        message, details = f"Browser error: {exc}", {"code": "BROWSER_ERROR"}
    except Exception as exc:  # a bug must not leave the job stuck "in progress"
        logger.exception("Browser execution failed at %s", ctx.tool)
        message = f"Unexpected error: {type(exc).__name__}: {exc}"
        details = {"code": "INTERNAL_ERROR"}
    steps.append({"step": ctx.tool, "error": message, "details": details})
    failed = fail_session(ctx, sid, message, details)
    if failed is None:  # the session had already ended or is waiting for the user
        session = ctx.db.get(BrowserSession, sid)
        assert session is not None
        failed = result(session, *_job_of(ctx, session))
    return {**failed, "error": message, "steps": steps, "details": details}


def _run_steps(ctx: ExecContext, sid: uuid.UUID, steps: list[dict[str, Any]]) -> dict[str, Any]:
    """inspect -> fill -> upload -> prepare -> (dry run) close. Any error is raised to
    ``run_execution``, which fails the session cleanly."""
    session = ctx.db.get(BrowserSession, sid)
    assert session is not None
    if session.state in (B.OPEN, B.FORM_DETECTED, B.FILLING):
        ctx.tool = "inspect_application_form"
        out = inspect_form(ctx, sid, click_apply=True)
        steps.append({"step": "inspect", "fields": len(out.get("fields") or [])})
        if out["blocked_needs_user"]:
            return {**out, "steps": steps}
    ctx.tool = "fill_application_field"
    out = fill_fields(ctx, sid, None)
    steps.append(
        {"step": "fill", "filled": len(out.get("filled") or []), "refused": out.get("refused")}
    )
    if out["blocked_needs_user"]:
        return {**out, "steps": steps}
    if any(f["key"] == "resume" for f in session.detected_fields or []):
        ctx.tool = "upload_application_resume"
        try:
            out = upload_resume(ctx, sid, None)
            upload = out.get("uploaded") or {}
            steps.append(
                {"step": "upload", "file": upload.get("file_name"), "status": upload.get("status")}
            )
        except ExecutionError as exc:
            steps.append({"step": "upload", "error": exc.message, "details": exc.details})
    ctx.tool = "prepare_submission"
    out = prepare_submission(ctx, sid)  # a form that is not ready fails the run (REQUIREMENTS 9)
    steps.append({"step": "prepare", "ready": out.get("ready")})
    if out["blocked_needs_user"] or ctx.mode is BrowserExecutionMode.LIVE:
        return {**out, "steps": steps}
    ctx.tool = "close_application_session"
    out = close_session(ctx, sid)
    steps.append({"step": "close"})
    return {**out, "steps": steps}


def _job_of(ctx: ExecContext, session: BrowserSession) -> tuple[Job]:
    job = ctx.db.get(Job, session.job_id)
    assert job is not None
    return (job,)
