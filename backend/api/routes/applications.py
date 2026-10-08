"""Applications and their browser execution (Phase 5).

The dashboard can run a DRY RUN (never submits), answer questions PIE could not, close a
session, and - only when EXTERNAL_LIVE_ENABLED=true - prepare a LIVE submission, confirm it
explicitly and submit it. Loading a page, starting the agent, creating a draft or generating
a resume is never treated as permission to submit.
"""

import uuid
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.api.deps import browser_dep, profile_dep
from backend.core.config import Settings, get_settings, live_execution_block
from backend.core.profile import UserProfile
from backend.services import execution as svc
from backend.services.platforms import Platform, platform_of
from backend.services.readiness import evaluate_readiness
from backend.services.statuses import UserStatus, user_status
from backend.services.tailoring import resume_lock_reason
from database.enums import BrowserExecutionMode
from database.models import (
    Application,
    ApplicationEvent,
    BrowserSession,
    ExternalActionLog,
    Job,
    SubmissionAttempt,
)
from database.session import get_db

router = APIRouter(prefix="/applications", tags=["applications"])
DB = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
ProfileDep = Annotated[UserProfile, Depends(profile_dep)]
BrowserDep = Annotated[Any, Depends(browser_dep)]

STATUS = {
    "NOT_FOUND": 404,
    "PRECONDITION_FAILED": 409,
    "DUPLICATE_APPLICATION": 409,
    "DAILY_LIMIT_REACHED": 429,
    "MODE_MISMATCH": 409,
    "VALIDATION_FAILED": 422,
    "USER_CONFIRMATION_REQUIRED": 428,
    "LIVE_NOT_ENABLED": 403,
    "DEVELOPMENT_MODE_BLOCKED": 403,
    "VISIBLE_BROWSER_REQUIRED": 403,
    "DRY_RUN_ONLY": 409,
    "UNEXPECTED_PAGE": 409,
    "BROWSER_ERROR": 502,
}


def _error(exc: svc.ExecutionError) -> JSONResponse:
    return JSONResponse(
        status_code=STATUS.get(exc.code, 400),
        content={"detail": exc.message, "code": exc.code, "details": exc.details},
    )


def _app(db: Session, app_id: uuid.UUID) -> Application:
    app = db.get(Application, app_id)
    if app is None:
        raise HTTPException(404, "Application not found")
    return app


def confirmation_statement(job: Job) -> str:
    company = job.company.name if job.company else "this employer"
    return (
        f"You are about to submit an application to {company} for {job.title}. "
        "This will perform an external action."
    )


def session_view(s: BrowserSession) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "mode": s.mode.value,
        "banner": svc.banner(s.mode),
        "state": s.state.value,
        "current_step": s.current_step,
        "target_url": s.target_url,
        "target_domain": s.target_domain,
        "current_url": s.current_url,
        "page_title": s.page_title,
        "fields": s.detected_fields or [],
        "filled_fields": [
            {k: f.get(k) for k in ("field_id", "label", "key", "source")}
            for f in s.filled_fields or []
        ],
        "skipped_fields": s.skipped_fields or [],
        "missing_answers": s.missing_answers or [],
        "challenges": s.challenges or [],
        "uploaded_resume": s.uploaded_resume,
        "screenshots": [
            {"index": i, "label": shot["label"], "taken_at": shot["taken_at"]}
            for i, shot in enumerate(s.screenshots or [])
        ],
        "errors": s.errors or [],
        "blocked_requests": len(s.blocked_requests or []),
        "user_action_required": s.user_action_required,
        "prepared_at": s.prepared_at,
        "user_confirmed_at": s.user_confirmed_at,
        "submission_evidence": s.submission_evidence,
        "dry_run_complete": s.mode is BrowserExecutionMode.DRY_RUN and s.prepared_at is not None,
        "created_at": s.created_at,
        "closed_at": s.closed_at,
    }


def _summary(db: Session, app: Application) -> dict[str, Any]:
    job = app.job
    latest = db.scalars(
        select(BrowserSession)
        .where(BrowserSession.application_id == app.id)
        .order_by(BrowserSession.created_at.desc())
    ).first()
    return {
        "id": str(app.id),
        "job_id": str(job.id),
        "company": job.company.name if job.company else None,
        "title": job.title,
        "application_status": app.status.value,
        "job_status": job.pipeline_state.value,
        "user_status": user_status(job.pipeline_state),
        "platform": platform_of(job),
        "resume_locked": resume_lock_reason(db, job, app) is not None,
        "requires_user_input": app.requires_user_input,
        "applied_at": app.applied_at,
        "updated_at": app.updated_at,
        "latest_session": None
        if latest is None
        else {
            "id": str(latest.id),
            "mode": latest.mode.value,
            "state": latest.state.value,
            "current_step": latest.current_step,
        },
    }


def execution_mode(settings: Settings) -> dict[str, Any]:
    """What external execution is allowed right now. Shown before any execution starts."""
    block = live_execution_block(settings)
    return {
        "development_mode": settings.development_mode,
        "external_live_configured": settings.external_live_enabled,
        "external_live_enabled": block is None,
        "live_block_reason": block[0] if block else None,
        "browser_headless": settings.browser_headless,
        "default_mode": "external_dry_run",
    }


@router.get("/mode")
def mode(settings: SettingsDep) -> dict[str, Any]:
    return execution_mode(settings)


@router.get("")
def list_applications(
    db: DB,
    settings: SettingsDep,
    platform: Platform | None = None,
    status_group: Annotated[list[UserStatus] | None, Query()] = None,
    updated_from: date | None = None,
    updated_to: date | None = None,
) -> list[dict[str, Any]]:
    """All applications, newest activity first. Filters: platform, the six statuses, and the
    date of the last change (your timezone)."""
    if updated_from and updated_to and updated_from > updated_to:
        raise HTTPException(422, "updated_from must not be after updated_to")
    zone = ZoneInfo(settings.timezone)
    stmt = select(Application).order_by(Application.updated_at.desc())
    if updated_from:
        stmt = stmt.where(
            Application.updated_at >= datetime.combine(updated_from, time.min, tzinfo=zone)
        )
    if updated_to:
        end = datetime.combine(updated_to + timedelta(days=1), time.min, tzinfo=zone)
        stmt = stmt.where(Application.updated_at < end)
    rows = [_summary(db, a) for a in db.scalars(stmt).all()]
    if platform:
        rows = [r for r in rows if r["platform"] == platform]
    if status_group:
        rows = [r for r in rows if r["user_status"] in status_group]
    return rows


def _resume_used(db: Session, app: Application) -> dict[str, Any] | None:
    version = app.resume_version
    if version is None:
        return None
    reason = resume_lock_reason(db, app.job, app)
    return {
        "version_id": str(version.id),
        "version_number": version.version_number,
        "is_master": version.is_master,
        "file_name": version.file_path.replace("\\", "/").rsplit("/", 1)[-1],
        "locked": reason is not None,
        "lock_reason": reason,
    }


@router.get("/{app_id}")
def application_detail(
    app_id: uuid.UUID, db: DB, settings: SettingsDep, profile: ProfileDep
) -> dict[str, Any]:
    app = _app(db, app_id)
    job = app.job
    sessions = db.scalars(
        select(BrowserSession)
        .where(BrowserSession.application_id == app.id)
        .order_by(BrowserSession.created_at.desc())
    ).all()
    logs = db.scalars(
        select(ExternalActionLog)
        .where(ExternalActionLog.application_id == app.id)
        .order_by(ExternalActionLog.occurred_at.desc())
        .limit(100)
    ).all()
    attempts = db.scalars(
        select(SubmissionAttempt).where(SubmissionAttempt.application_id == app.id)
    ).all()
    events = db.scalars(
        select(ApplicationEvent)
        .where(ApplicationEvent.job_id == job.id)
        .order_by(ApplicationEvent.occurred_at.desc())
        .limit(100)
    ).all()
    return {
        **_summary(db, app),
        "url": job.url,
        "application_url": job.application_url,
        "user_action_required": app.user_action_required,
        "last_error": app.last_error,
        "resume": _resume_used(db, app),
        "readiness": evaluate_readiness(db, settings, profile, job, app).as_dict(),
        **execution_mode(settings),
        "confirmation_statement": confirmation_statement(job),
        "sessions": [session_view(s) for s in sessions],
        "external_actions": [
            {
                "at": log.occurred_at,
                "tool": log.tool,
                "mode": log.mode,
                "actor": log.actor,
                "domain": log.target_domain,
                "summary": log.summary,
                "result": log.result.value,
                "error": log.error,
                "run_id": str(log.run_id) if log.run_id else None,
            }
            for log in logs
        ],
        "submission_attempts": [
            {
                "id": str(a.id),
                "attempted_at": a.attempted_at,
                "outcome": a.outcome.value,
                "evidence": a.evidence,
            }
            for a in attempts
        ],
        "timeline": [
            {
                "at": e.occurred_at,
                "event_type": e.event_type,
                "source": e.source.value,
                "previous": e.previous_status,
                "new": e.new_status,
                "reason": e.reason,
            }
            for e in events
        ],
    }


def _ctx(
    db: Session, settings: Settings, profile: UserProfile, browser: Any, live: bool
) -> svc.ExecContext:
    return svc.ExecContext(
        db=db,
        settings=settings,
        profile=profile,
        browser=browser,
        mode=BrowserExecutionMode.LIVE if live else BrowserExecutionMode.DRY_RUN,
        actor="user",
    )


@router.post("/{app_id}/dry-run", response_model=None)
def dry_run(
    app_id: uuid.UUID, db: DB, settings: SettingsDep, profile: ProfileDep, browser: BrowserDep
) -> Any:
    """Run the whole application in DRY RUN mode. Nothing is submitted."""
    app = _app(db, app_id)
    try:
        return svc.run_execution(_ctx(db, settings, profile, browser, False), app.job_id)
    except svc.ExecutionError as exc:
        return _error(exc)


@router.post("/{app_id}/live/prepare", response_model=None)
def live_prepare(
    app_id: uuid.UUID, db: DB, settings: SettingsDep, profile: ProfileDep, browser: BrowserDep
) -> Any:
    """LIVE (only if enabled): fill and validate, then WAIT for the user's confirmation."""
    try:
        svc.check_live(settings)  # DEVELOPMENT_MODE, live flag and visible browser, first
    except svc.ExecutionError as exc:
        return _error(exc)
    app = _app(db, app_id)
    try:
        return svc.run_execution(_ctx(db, settings, profile, browser, True), app.job_id)
    except svc.ExecutionError as exc:
        return _error(exc)


class Answer(BaseModel):
    question_key: str = Field(min_length=1, max_length=200)
    question: str = Field(default="", max_length=2000)
    answer: str = Field(min_length=1, max_length=2000)
    remember: bool = False  # reuse for every application


class AnswersRequest(BaseModel):
    answers: list[Answer] = Field(min_length=1, max_length=50)


@router.post("/{app_id}/answers")
def answer_questions(app_id: uuid.UUID, body: AnswersRequest, db: DB) -> dict[str, Any]:
    """Your answers to questions the profile did not cover (stored as VERIFIED, source: you)."""
    app = _app(db, app_id)
    rows = svc.provide_answers(db, app, [a.model_dump() for a in body.answers])
    return {"saved": len(rows), "note": "Resume the dry run (or the agent run) to continue."}


def _session_of(db: Session, app_id: uuid.UUID, session_id: uuid.UUID) -> BrowserSession:
    s = db.get(BrowserSession, session_id)
    if s is None or s.application_id != app_id:
        raise HTTPException(404, "Browser session not found")
    return s


@router.post("/{app_id}/sessions/{session_id}/close", response_model=None)
def close(
    app_id: uuid.UUID,
    session_id: uuid.UUID,
    db: DB,
    settings: SettingsDep,
    profile: ProfileDep,
    browser: BrowserDep,
) -> Any:
    s = _session_of(db, app_id, session_id)
    ctx = _ctx(db, settings, profile, browser, s.mode is BrowserExecutionMode.LIVE)
    ctx.tool = "close_application_session"
    try:
        return svc.close_session(ctx, s.id)
    except svc.ExecutionError as exc:
        return _error(exc)


class ConfirmRequest(BaseModel):
    # The exact statement shown to the user; must match the server's text for this job.
    acknowledged_statement: str = Field(min_length=10, max_length=1000)


@router.post("/{app_id}/sessions/{session_id}/confirm-live", response_model=None)
def confirm_live(
    app_id: uuid.UUID, session_id: uuid.UUID, body: ConfirmRequest, db: DB, settings: SettingsDep
) -> Any:
    s = _session_of(db, app_id, session_id)
    job = db.get(Job, s.job_id)
    assert job is not None
    expected = confirmation_statement(job)
    if body.acknowledged_statement.strip() != expected:
        raise HTTPException(422, "The confirmation must acknowledge the exact statement shown")
    try:
        svc.confirm_live_submission(db, settings, s.id, expected)
    except svc.ExecutionError as exc:
        return _error(exc)
    return {"confirmed": True, "statement": expected}


@router.post("/{app_id}/sessions/{session_id}/submit", response_model=None)
def submit(
    app_id: uuid.UUID,
    session_id: uuid.UUID,
    db: DB,
    settings: SettingsDep,
    profile: ProfileDep,
    browser: BrowserDep,
) -> Any:
    """LIVE submit of a prepared, user-confirmed session. Refused for dry runs."""
    s = _session_of(db, app_id, session_id)
    ctx = _ctx(db, settings, profile, browser, s.mode is BrowserExecutionMode.LIVE)
    ctx.tool = "submit_application"
    try:
        return svc.submit(ctx, s.id)
    except svc.ExecutionError as exc:
        return _error(exc)


class ResolveRequest(BaseModel):
    submitted: bool
    note: str = Field(min_length=3, max_length=1000)


@router.post("/{app_id}/sessions/{session_id}/resolve", response_model=None)
def resolve(
    app_id: uuid.UUID,
    session_id: uuid.UUID,
    body: ResolveRequest,
    db: DB,
    settings: SettingsDep,
    profile: ProfileDep,
    browser: BrowserDep,
) -> Any:
    """You settle an unconfirmed live submission after checking email / the employer portal."""
    s = _session_of(db, app_id, session_id)
    ctx = _ctx(db, settings, profile, browser, True)
    ctx.tool = "resolve_submission"
    try:
        return svc.resolve_submission(ctx, s.id, body.submitted, body.note)
    except svc.ExecutionError as exc:
        return _error(exc)


@router.get("/{app_id}/sessions/{session_id}/screenshots/{index}")
def screenshot(
    app_id: uuid.UUID, session_id: uuid.UUID, index: int, db: DB, settings: SettingsDep
) -> FileResponse:
    s = _session_of(db, app_id, session_id)
    shots = s.screenshots or []
    if not 0 <= index < len(shots):
        raise HTTPException(404, "Screenshot not found")
    root = settings.application_artifacts_dir.resolve()
    path = (root / shots[index]["file"]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404, "Screenshot not found")
    return FileResponse(path, media_type="image/png")
