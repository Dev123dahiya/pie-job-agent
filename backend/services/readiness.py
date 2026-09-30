"""Application readiness: what stands between a shortlisted job and READY_TO_APPLY.

Two kinds of gaps:
- user_blockers: only the user can resolve them (missing verified answers, no master resume)
  -> the job moves to BLOCKED_NEEDS_USER
- pending_steps: PIE/the agent can do them (create a draft, tailor a validated resume)
  -> the job stays SHORTLISTED
READY_TO_APPLY requires neither. Nothing here submits anything.
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from backend.core.config import Settings
from backend.core.profile import UserProfile
from backend.services.resumes import locate_master_resume
from database.enums import EventSource, JobPipelineState, ResumeValidationStatus
from database.models import Application, ApplicationEvent, Job, ResumeVersion
from database.state_machine import assert_transition

S = JobPipelineState
ELIGIBLE = {S.SHORTLISTED, S.READY_TO_APPLY, S.BLOCKED_NEEDS_USER}

# Questions most applications ask. Missing answers are asked of the user, never guessed.
STANDARD_QUESTIONS: dict[str, str] = {
    "work_authorization": "Are you legally authorized to work in India (and where needed, abroad)?",
    "notice_period": "What is your notice period?",
    "expected_salary": "What is your expected salary?",
    "willing_to_relocate": "Are you willing to relocate?",
}


@dataclass
class Readiness:
    eligible: bool
    user_blockers: list[str] = field(default_factory=list)
    pending_steps: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.eligible and not self.user_blockers and not self.pending_steps

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "eligible": self.eligible,
            "user_blockers": self.user_blockers,
            "pending_steps": self.pending_steps,
        }


def user_blockers(settings: Settings, profile: UserProfile) -> list[str]:
    blockers = []
    if locate_master_resume(settings) is None:
        blockers.append(
            "Master resume not available to PIE: put it in resumes/master/ or set "
            "MASTER_RESUME_PATH."
        )
    known = set(profile.verified_profile.verified_answers)
    blockers += [
        f"Missing verified answer '{k}': {q}"
        for k, q in STANDARD_QUESTIONS.items()
        if k not in known
    ]
    return blockers


def validated_resume_for(db: Session, job: Job, app: Application | None) -> ResumeVersion | None:
    if app is None or app.resume_version_id is None:
        return None
    version = db.get(ResumeVersion, app.resume_version_id)
    if version is None or version.job_id != job.id:
        return None
    return version if version.validation_status is ResumeValidationStatus.VALIDATED else None


def evaluate_readiness(
    db: Session, settings: Settings, profile: UserProfile, job: Job, app: Application | None
) -> Readiness:
    result = Readiness(eligible=job.pipeline_state in ELIGIBLE)
    if not result.eligible:
        result.pending_steps.append(
            f"Job is {job.pipeline_state.value}: only shortlisted jobs can get an application"
        )
    result.user_blockers = user_blockers(settings, profile)
    if app is None:
        result.pending_steps.append("Create an application draft (create_application_draft)")
    elif validated_resume_for(db, job, app) is None:
        result.pending_steps.append(
            "Create a validated tailored resume for this job (create_tailored_resume)"
        )
    return result


def apply_readiness(
    db: Session,
    job: Job,
    app: Application,
    readiness: Readiness,
    *,
    actor: str = "agent",
    state_changes: list[Any] | None = None,
) -> None:
    """Record readiness on the draft and move the job to the matching state."""
    app.requires_user_input = bool(readiness.user_blockers)
    app.user_action_required = "\n".join(readiness.user_blockers) or None
    app.next_action = (
        readiness.pending_steps[0]
        if readiness.pending_steps
        else (None if readiness.ready else app.next_action)
    )
    if not readiness.eligible:
        return
    if readiness.user_blockers:
        target, reason = S.BLOCKED_NEEDS_USER, "Needs information only the user can provide"
    elif readiness.ready:
        target, reason = S.READY_TO_APPLY, "Draft, validated tailored resume and answers complete"
    else:
        target, reason = S.SHORTLISTED, "Waiting for: " + "; ".join(readiness.pending_steps)
    previous = job.pipeline_state
    if previous is target:
        return
    assert_transition(previous, target)
    job.pipeline_state = target
    db.add(
        ApplicationEvent(
            job_id=job.id,
            application_id=app.id,
            event_type="readiness_changed",
            source=EventSource.USER if actor == "user" else EventSource.AGENT,
            previous_status=previous.value,
            new_status=target.value,
            reason=reason,
            evidence=readiness.as_dict(),
        )
    )
    if state_changes is not None:
        from agent.tools.base import StateChange

        state_changes.append(StateChange("job", str(job.id), previous.value, target.value, reason))
