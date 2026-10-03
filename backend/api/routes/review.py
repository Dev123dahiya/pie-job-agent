"""Review queue (REQUIREMENTS 10 "review" mode, 19.2.3): every LIVE application prepared in the
browser and waiting for the user's yes/no.

Approve = the user's explicit confirmation of the exact statement for that job, then the
existing live submit with all its checks (live switches, duplicates, daily limits, form
unchanged, one submission ever). Reject = close the session without submitting and withdraw
the job. Nothing here can submit without the user's confirmation, one job at a time or many.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.api.deps import browser_dep, platform_browser_dep, profile_dep
from backend.api.routes.applications import confirmation_statement
from backend.core.config import Settings, get_settings
from backend.core.profile import UserProfile
from backend.services import execution as svc
from backend.services.platforms import platform_of
from database.base import utcnow
from database.enums import BrowserExecutionMode, BrowserSessionState, EventSource
from database.enums import JobPipelineState as S
from database.models import Application, ApplicationEvent, BrowserSession, Job, JobAnalysis
from database.session import get_db
from database.state_machine import assert_transition

router = APIRouter(prefix="/review", tags=["review"])
DB = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
ProfileDep = Annotated[UserProfile, Depends(profile_dep)]
BrowserDep = Annotated[Any, Depends(browser_dep)]
PlatformBrowserDep = Annotated[Any, Depends(platform_browser_dep)]


def _ctx(db: Session, settings: Settings, profile: UserProfile, browser: Any) -> svc.ExecContext:
    return svc.ExecContext(
        db=db,
        settings=settings,
        profile=profile,
        browser=browser,
        mode=BrowserExecutionMode.LIVE,
        actor="user",
    )


def _waiting(db: Session, session_id: uuid.UUID) -> tuple[BrowserSession, Job]:
    s = db.get(BrowserSession, session_id)
    if s is None or s.mode is not BrowserExecutionMode.LIVE:
        raise HTTPException(404, "No live application waiting for review with this id")
    if s.state is not BrowserSessionState.READY_FOR_SUBMISSION:
        raise HTTPException(409, f"This application is {s.state.value}, not waiting for review")
    job = db.get(Job, s.job_id)
    assert job is not None
    return s, job


@router.get("")
def queue(db: DB) -> dict[str, Any]:
    sessions = db.scalars(
        select(BrowserSession)
        .where(
            BrowserSession.mode == BrowserExecutionMode.LIVE,
            BrowserSession.state == BrowserSessionState.READY_FOR_SUBMISSION,
        )
        .order_by(BrowserSession.prepared_at)
    ).all()
    items = []
    for s in sessions:
        job = db.get(Job, s.job_id)
        app = db.get(Application, s.application_id)
        if job is None or app is None:
            continue
        analysis = db.scalars(
            select(JobAnalysis)
            .where(JobAnalysis.job_id == job.id)
            .order_by(JobAnalysis.created_at.desc())
        ).first()
        version = app.resume_version
        items.append(
            {
                "session_id": str(s.id),
                "application_id": str(app.id),
                "job_id": str(job.id),
                "title": job.title,
                "company": job.company.name if job.company else None,
                "platform": platform_of(job),
                "url": job.application_url or job.url,
                "match_score": analysis.match_score if analysis else None,
                "match_reason": analysis.reason if analysis else None,
                "resume": {
                    "version_id": str(version.id),
                    "version_number": version.version_number,
                }
                if version
                else None,
                # What will be sent: each field's label and where its answer comes from. The
                # values stay out of PIE's records (they are on the page itself).
                "answers": [
                    {
                        "label": f.get("label"),
                        "key": f.get("key"),
                        "source": f.get("source"),
                        "ai_suggested": "claude" in str(f.get("source") or "").lower(),
                    }
                    for f in s.filled_fields or []
                ],
                "uploaded_resume": (s.uploaded_resume or {}).get("file_name"),
                "prepared_at": s.prepared_at,
                "confirmation_statement": confirmation_statement(job),
            }
        )
    return {"items": items, "total": len(items)}


class Approve(BaseModel):
    statement: str = Field(min_length=10, max_length=1000)


def _approve(
    db: Session,
    settings: Settings,
    profile: UserProfile,
    browser: Any,
    sid: uuid.UUID,
    text: str,
    platform_browser: Any = None,
) -> dict[str, Any]:
    s, job = _waiting(db, sid)
    expected = confirmation_statement(job)
    if text.strip() != expected:
        raise HTTPException(422, "The approval must acknowledge the exact statement shown")
    ctx = _ctx(db, settings, profile, browser)
    try:
        svc.confirm_live_submission(db, settings, s.id, expected)
        if platform_of(job) == "naukri":
            from backend.services import naukri_apply

            db.refresh(s)
            out = naukri_apply.submit(db, settings, profile, platform_browser("naukri"), s)
            return {"session_id": str(sid), **out}
        if platform_of(job) == "linkedin":
            from backend.services import linkedin_apply

            db.refresh(s)
            li_ctx = _ctx(db, settings, profile, platform_browser("linkedin"))
            out = linkedin_apply.submit(li_ctx, s)
            return {"session_id": str(sid), **out}
        ctx.tool = "submit_application"
        out = svc.submit(ctx, s.id)
    except svc.ExecutionError as exc:
        return {"session_id": str(sid), "submitted": False, "code": exc.code, "error": exc.message}
    return {
        "session_id": str(sid),
        "submitted": bool(out.get("submitted")),
        "outcome": out.get("outcome"),
        "state": out.get("state"),
    }


@router.post("/{session_id}/approve")
def approve(
    session_id: uuid.UUID,
    body: Approve,
    db: DB,
    settings: SettingsDep,
    profile: ProfileDep,
    browser: BrowserDep,
    platform_browser: PlatformBrowserDep,
) -> dict[str, Any]:
    """Yes: submit this one application (after every live check)."""
    return _approve(db, settings, profile, browser, session_id, body.statement, platform_browser)


class ApproveItem(BaseModel):
    session_id: uuid.UUID
    statement: str = Field(min_length=10, max_length=1000)


class ApproveBatch(BaseModel):
    items: list[ApproveItem] = Field(min_length=1, max_length=20)


@router.post("/approve-batch")
def approve_batch(
    body: ApproveBatch,
    db: DB,
    settings: SettingsDep,
    profile: ProfileDep,
    browser: BrowserDep,
    platform_browser: PlatformBrowserDep,
) -> dict[str, Any]:
    """Yes to several, each with its own exact statement. Each is submitted on its own; one
    refusal (e.g. the daily limit) is reported and never skips another's checks."""
    results = []
    for item in body.items:
        try:
            results.append(
                _approve(
                    db,
                    settings,
                    profile,
                    browser,
                    item.session_id,
                    item.statement,
                    platform_browser,
                )  # fmt: skip
            )
        except HTTPException as exc:
            results.append(
                {"session_id": str(item.session_id), "submitted": False, "error": exc.detail}
            )
    return {"results": results, "submitted": sum(bool(r.get("submitted")) for r in results)}


@router.post("/{session_id}/reject")
def reject(
    session_id: uuid.UUID, db: DB, settings: SettingsDep, profile: ProfileDep, browser: BrowserDep
) -> dict[str, Any]:
    """No: close the form without submitting, and withdraw the job."""
    s, job = _waiting(db, session_id)
    ctx = _ctx(db, settings, profile, browser)
    ctx.tool = "close_application_session"
    svc.close_session(ctx, s.id)
    db.refresh(job)
    if job.pipeline_state is S.READY_TO_APPLY:
        assert_transition(job.pipeline_state, S.WITHDRAWN)
        job.pipeline_state = S.WITHDRAWN
        db.add(
            ApplicationEvent(
                job_id=job.id,
                application_id=s.application_id,
                occurred_at=utcnow(),
                event_type="rejected_in_review",
                source=EventSource.USER,
                previous_status=S.READY_TO_APPLY.value,
                new_status=S.WITHDRAWN.value,
                reason="You rejected it in the review queue; nothing was submitted",
            )
        )
        db.commit()
    return {"session_id": str(session_id), "status": job.pipeline_state.value}
