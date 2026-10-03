"""The user's Skip and Move-to-shortlist actions on a job (REQUIREMENTS 19.2.2).

Both are recorded in the job's timeline as the user's decision. Neither opens a browser or
sends anything. Move-to-shortlist is the user overriding a shortlist rule (say, location);
it never overrides the minimum match score (REQUIREMENTS 6, 19.1): a job without a Claude
score at or above min_match_score cannot be shortlisted from the dashboard.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.services.shortlist import ShortlistRules
from database.enums import EventSource
from database.enums import JobPipelineState as S
from database.models import Application, ApplicationEvent, Job, JobAnalysis
from database.state_machine import assert_transition

DEFAULT_MIN_MATCH_SCORE = 70  # REQUIREMENTS 6
SKIPPABLE = {S.DISCOVERED, S.ANALYZED, S.SHORTLISTED, S.FAILED}
SHORTLISTABLE = {S.ANALYZED, S.REJECTED_BY_AGENT}


class JobActionError(ValueError):
    """The action is not allowed for this job; the message says why."""


def _job(db: Session, job_id: uuid.UUID) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise JobActionError(f"No job {job_id}")
    return job


def _record(db: Session, job: Job, target: S, event_type: str, reason: str) -> None:
    previous = job.pipeline_state
    assert_transition(previous, target)
    job.pipeline_state = target
    app = db.scalar(select(Application).where(Application.job_id == job.id))
    db.add(
        ApplicationEvent(
            job_id=job.id,
            application_id=app.id if app else None,
            event_type=event_type,
            source=EventSource.USER,
            previous_status=previous.value,
            new_status=target.value,
            reason=reason,
        )
    )


def skip_job(db: Session, job_id: uuid.UUID, reason: str | None = None) -> Job:
    """Mark a job as skipped (REJECTED_BY_AGENT, shown as "skipped"). Only before any
    application work has started; it can be shortlisted again later."""
    job = _job(db, job_id)
    if job.pipeline_state not in SKIPPABLE:
        raise JobActionError(
            f"Job is {job.pipeline_state.value}: skip works only before an application starts "
            "(found, analyzed, shortlisted or failed)."
        )
    _record(db, job, S.REJECTED_BY_AGENT, "skipped_by_user", reason or "Skipped by you")
    db.commit()
    return job


def shortlist_job(db: Session, job_id: uuid.UUID, rules: ShortlistRules) -> Job:
    job = _job(db, job_id)
    if job.pipeline_state not in SHORTLISTABLE:
        raise JobActionError(
            f"Job is {job.pipeline_state.value}: only analyzed or skipped jobs can be moved "
            "to the shortlist."
        )
    latest = db.scalars(
        select(JobAnalysis)
        .where(JobAnalysis.job_id == job.id)
        .order_by(JobAnalysis.created_at.desc())
    ).first()
    minimum = (
        rules.min_match_score if rules.min_match_score is not None else DEFAULT_MIN_MATCH_SCORE
    )
    if latest is None or latest.match_score is None:
        raise JobActionError("This job has no match score yet. Analyze it first.")
    if latest.match_score < minimum:
        raise JobActionError(
            f"Match score {latest.match_score} is below the minimum of {minimum}; the "
            "dashboard can't override the minimum score."
        )
    _record(db, job, S.SHORTLISTED, "shortlisted_by_user", "Moved to the shortlist by you")
    db.commit()
    return job
