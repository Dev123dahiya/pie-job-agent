"""Failed jobs: listed for the user and retried only by the user (REQUIREMENTS 12).

A job is FAILED only when its application failed in the browser (a failed *analysis* leaves
the job DISCOVERED with ``last_error`` set). PIE never re-analyzes, re-shortlists or re-runs
a FAILED job on its own; ``retry_failed_job`` is the one way back, and it only puts the job
back in the queue. Nothing is opened or submitted by a retry.
"""

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.config import Settings
from backend.core.profile import UserProfile
from backend.services.readiness import apply_readiness, evaluate_readiness
from database.enums import BrowserSessionState, EventSource
from database.enums import JobPipelineState as S
from database.models import Application, ApplicationEvent, BrowserSession, Job
from database.state_machine import assert_transition

RETRIED_EVENT = "retried_by_user"
FAILED_EVENT = "execution_failed"
ACTIVE_SESSION_STATES = {
    BrowserSessionState.CREATED,
    BrowserSessionState.OPEN,
    BrowserSessionState.NAVIGATING,
    BrowserSessionState.FORM_DETECTED,
    BrowserSessionState.FILLING,
    BrowserSessionState.WAITING_FOR_USER,
    BrowserSessionState.READY_FOR_SUBMISSION,
}


class RetryError(ValueError):
    """The job cannot be retried (not found, not failed, or a browser session is open)."""


@dataclass
class FailedJob:
    job_id: str
    title: str
    company: str | None
    url: str
    error: str | None
    stage: str | None  # the step it failed at, e.g. fill_application_field
    failed_at: datetime | None
    screenshot: str | None  # relative to APPLICATION_ARTIFACTS_DIR
    retries: int

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["failed_at"] = self.failed_at.isoformat() if self.failed_at else None
        return data


def list_failed_jobs(db: Session, limit: int = 200) -> list[FailedJob]:
    jobs = db.scalars(
        select(Job)
        .where(Job.pipeline_state == S.FAILED)
        .order_by(Job.updated_at.desc())
        .limit(limit)
    ).all()
    out = []
    for job in jobs:
        failure = db.scalars(
            select(ApplicationEvent)
            .where(ApplicationEvent.job_id == job.id, ApplicationEvent.event_type == FAILED_EVENT)
            .order_by(ApplicationEvent.occurred_at.desc())
        ).first()
        session = db.scalars(
            select(BrowserSession)
            .where(BrowserSession.job_id == job.id)
            .order_by(BrowserSession.created_at.desc())
        ).first()
        shot = next(
            (s["file"] for s in reversed(session.screenshots or []) if s["label"] == "failed"),
            None,
        ) if session else None  # fmt: skip
        app = db.scalar(select(Application).where(Application.job_id == job.id))
        retries = db.scalar(
            select(func.count())
            .select_from(ApplicationEvent)
            .where(ApplicationEvent.job_id == job.id, ApplicationEvent.event_type == RETRIED_EVENT)
        )
        out.append(
            FailedJob(
                job_id=str(job.id),
                title=job.title,
                company=job.company.name if job.company else None,
                url=job.application_url or job.url,
                error=(app.last_error if app else None) or job.last_error,
                stage=(failure.evidence or {}).get("stage") if failure else None,
                failed_at=failure.occurred_at if failure else None,
                screenshot=shot,
                retries=int(retries or 0),
            )
        )
    return out


def retry_failed_job(
    db: Session, settings: Settings, profile: UserProfile, job_id: uuid.UUID
) -> Job:
    """Put a FAILED job back in the queue, as the user asked. It becomes READY_TO_APPLY if
    its draft, validated resume and answers are still complete, otherwise SHORTLISTED or
    BLOCKED_NEEDS_USER (the normal readiness rules). The analysis is not re-run."""
    job = db.get(Job, job_id)
    if job is None:
        raise RetryError(f"No job {job_id}")
    if job.pipeline_state is not S.FAILED:
        raise RetryError(f"Job is {job.pipeline_state.value}; only FAILED jobs can be retried")
    app = db.scalar(select(Application).where(Application.job_id == job.id))
    if app is None:
        raise RetryError("This job has no application draft to retry")
    open_session = db.scalar(
        select(BrowserSession.id).where(
            BrowserSession.job_id == job.id, BrowserSession.state.in_(ACTIVE_SESSION_STATES)
        )
    )
    if open_session is not None:
        raise RetryError(f"Browser session {open_session} is still open; close it first")

    previous_error = app.last_error or job.last_error
    assert_transition(job.pipeline_state, S.SHORTLISTED)
    job.pipeline_state = S.SHORTLISTED
    app.last_error = None
    db.add(
        ApplicationEvent(
            job_id=job.id,
            application_id=app.id,
            event_type=RETRIED_EVENT,
            source=EventSource.USER,
            previous_status=S.FAILED.value,
            new_status=S.SHORTLISTED.value,
            reason="Retried by the user",
            evidence={"previous_error": (previous_error or "")[:500]},
        )
    )
    apply_readiness(db, job, app, evaluate_readiness(db, settings, profile, job, app), actor="user")
    db.commit()
    return job
