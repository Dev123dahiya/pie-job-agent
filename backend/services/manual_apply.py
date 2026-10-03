"""Manual-apply list (REQUIREMENTS 9, 19.2.6): jobs the user applies to themselves.

A job lands here when its apply link goes to a site PIE can't fill (the platform modules call
``send_to_manual``), or when the user chooses to apply themselves. PIE only tracks it: the user
marks it applied or skips it. Nothing is opened or submitted by PIE for these jobs, and they
don't use up PIE's daily limits (those count PIE's own submissions).
"""

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.services.failed_jobs import ACTIVE_SESSION_STATES
from database.base import utcnow
from database.enums import ApplicationStatus, EventSource
from database.enums import JobPipelineState as S
from database.models import Application, ApplicationEvent, BrowserSession, Job, JobAnalysis
from database.state_machine import ALLOWED_TRANSITIONS, assert_transition

SENT_EVENT = "moved_to_manual_apply"
APPLIED_EVENT = "applied_manually_by_user"


class ManualApplyError(ValueError):
    """The action is not allowed for this job; the message says why."""


@dataclass
class ManualJob:
    job_id: str
    title: str
    company: str | None
    url: str  # where to apply: the application link if known, else the posting
    match_score: int | None
    reason: str | None
    added_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["added_at"] = self.added_at.isoformat() if self.added_at else None
        return data


def _job(db: Session, job_id: uuid.UUID) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise ManualApplyError(f"No job {job_id}")
    return job


def _move(
    db: Session, job: Job, target: S, event_type: str, reason: str, source: EventSource
) -> Application | None:
    previous = job.pipeline_state
    assert_transition(previous, target)
    job.pipeline_state = target
    app = db.scalar(select(Application).where(Application.job_id == job.id))
    db.add(
        ApplicationEvent(
            job_id=job.id,
            application_id=app.id if app else None,
            event_type=event_type,
            source=source,
            previous_status=previous.value,
            new_status=target.value,
            reason=reason,
        )
    )
    return app


def send_to_manual(
    db: Session,
    job_id: uuid.UUID,
    reason: str = "You chose to apply yourself",
    source: EventSource = EventSource.USER,
) -> Job:
    job = _job(db, job_id)
    if S.MANUAL_APPLY not in ALLOWED_TRANSITIONS.get(job.pipeline_state, set()):
        raise ManualApplyError(
            f"Job is {job.pipeline_state.value}: only shortlisted, ready, paused or failed jobs "
            "can be moved to the manual-apply list."
        )
    open_session = db.scalar(
        select(BrowserSession.id).where(
            BrowserSession.job_id == job.id,
            BrowserSession.state.in_(ACTIVE_SESSION_STATES),
        )
    )
    if open_session is not None:
        raise ManualApplyError("A browser session is still open for this job; close it first")
    _move(db, job, S.MANUAL_APPLY, SENT_EVENT, reason[:500], source)
    db.commit()
    return job


def list_manual(db: Session) -> list[ManualJob]:
    jobs = db.scalars(
        select(Job).where(Job.pipeline_state == S.MANUAL_APPLY).order_by(Job.updated_at.desc())
    ).all()
    out = []
    for job in jobs:
        analysis = db.scalars(
            select(JobAnalysis)
            .where(JobAnalysis.job_id == job.id)
            .order_by(JobAnalysis.created_at.desc())
        ).first()
        sent = db.scalars(
            select(ApplicationEvent)
            .where(ApplicationEvent.job_id == job.id, ApplicationEvent.event_type == SENT_EVENT)
            .order_by(ApplicationEvent.occurred_at.desc())
        ).first()
        out.append(
            ManualJob(
                job_id=str(job.id),
                title=job.title,
                company=job.company.name if job.company else None,
                url=job.application_url or job.url,
                match_score=analysis.match_score if analysis else None,
                reason=sent.reason if sent else None,
                added_at=sent.occurred_at if sent else job.updated_at,
            )
        )
    return out


def mark_applied(db: Session, job_id: uuid.UUID) -> Job:
    """The user says they applied themselves: tracked like any other application."""
    job = _job(db, job_id)
    if job.pipeline_state is not S.MANUAL_APPLY:
        raise ManualApplyError(f"Job is {job.pipeline_state.value}, not on the manual-apply list")
    app = _move(db, job, S.SUBMITTED, APPLIED_EVENT, "You applied yourself", EventSource.USER)
    if app is None:
        app = Application(job_id=job.id)
        db.add(app)
    app.status = ApplicationStatus.APPLIED
    app.applied_at = utcnow()
    app.requires_user_input, app.user_action_required, app.next_action = False, None, None
    app.notes = ((app.notes + "\n") if app.notes else "") + "Applied manually by you"
    db.commit()
    return job


def skip_manual(db: Session, job_id: uuid.UUID) -> Job:
    job = _job(db, job_id)
    if job.pipeline_state is not S.MANUAL_APPLY:
        raise ManualApplyError(f"Job is {job.pipeline_state.value}, not on the manual-apply list")
    _move(db, job, S.REJECTED_BY_AGENT, "skipped_by_user", "Skipped by you", EventSource.USER)
    db.commit()
    return job
