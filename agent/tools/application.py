"""Application tools. Drafts are internal PIE records only: nothing is sent to an employer.

READY_TO_APPLY needs: a SHORTLISTED job, a draft, a VALIDATED tailored resume for the job,
the master resume, and every standard verified answer (see backend/services/readiness.py).
APPLICATION_IN_PROGRESS, READY_FOR_SUBMISSION and SUBMITTED are set only by the browser
execution tools (agent/tools/execution.py); no tool here can set them.
"""

import uuid
from typing import Any

from pydantic import Field, model_validator
from sqlalchemy import select

from agent.tools.base import (
    StateChange,
    Tool,
    ToolContext,
    ToolError,
    ToolErrorCode,
    ToolInput,
    ToolOutput,
)
from agent.tools.jobs import get_job_or_error
from backend.services.readiness import apply_readiness, evaluate_readiness
from database.enums import (
    ApplicationStatus,
    EventSource,
    JobPipelineState,
    ResumeValidationStatus,
    ToolPermission,
)
from database.models import Application, ApplicationEvent, Job, ResumeVersion
from database.state_machine import assert_transition

S = JobPipelineState


def _move(
    ctx: ToolContext,
    job: Job,
    target: JobPipelineState,
    reason: str,
    event_type: str,
    application: Application | None,
    evidence: dict[str, Any] | None = None,
) -> None:
    previous = job.pipeline_state
    assert_transition(previous, target)  # raises InvalidTransitionError -> INVALID_TRANSITION
    job.pipeline_state = target
    ctx.db.add(
        ApplicationEvent(
            job_id=job.id,
            application_id=application.id if application else None,
            event_type=event_type,
            source=EventSource.USER if ctx.actor == "user" else EventSource.AGENT,
            previous_status=previous.value,
            new_status=target.value,
            reason=reason,
            evidence=evidence,
        )
    )
    ctx.state_changes.append(StateChange("job", str(job.id), previous.value, target.value, reason))


class AppLookupInput(ToolInput):
    application_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "AppLookupInput":
        if (self.application_id is None) == (self.job_id is None):
            raise ValueError("Provide exactly one of application_id or job_id")
        return self


class ApplicationOutput(ToolOutput):
    application_id: str
    job_id: str
    company: str | None
    title: str
    application_status: str
    job_status: str
    tailored_resume_version: int | None
    requires_user_input: bool
    user_action_required: str | None
    readiness: dict[str, Any]
    applied_at: str | None
    timeline: list[dict[str, Any]]


def _version_number(ctx: ToolContext, app: Application) -> int | None:
    if app.resume_version_id is None:
        return None
    version = ctx.db.get(ResumeVersion, app.resume_version_id)
    return version.version_number if version else None


def _application_out(ctx: ToolContext, app: Application) -> ApplicationOutput:
    job = app.job
    events = ctx.db.scalars(
        select(ApplicationEvent)
        .where(ApplicationEvent.job_id == job.id)
        .order_by(ApplicationEvent.occurred_at)
    ).all()
    readiness = evaluate_readiness(ctx.db, ctx.settings, ctx.profile, job, app)
    return ApplicationOutput(
        application_id=str(app.id),
        job_id=str(job.id),
        company=job.company.name if job.company else None,
        title=job.title,
        application_status=app.status.value,
        job_status=job.pipeline_state.value,
        tailored_resume_version=_version_number(ctx, app),
        requires_user_input=app.requires_user_input,
        user_action_required=app.user_action_required,
        readiness=readiness.as_dict(),
        applied_at=app.applied_at.isoformat() if app.applied_at else None,
        timeline=[
            {
                "at": e.occurred_at.isoformat(),
                "event": e.event_type,
                "status": e.new_status,
                "reason": e.reason,
            }  # fmt: skip
            for e in events[-15:]
        ],
    )


def _find_application(ctx: ToolContext, p: AppLookupInput) -> Application:
    if p.application_id is not None:
        app = ctx.db.get(Application, p.application_id)
    else:
        app = ctx.db.scalar(select(Application).where(Application.job_id == p.job_id))
    if app is None:
        raise ToolError(ToolErrorCode.NOT_FOUND, "No application found")
    return app


def get_application(ctx: ToolContext, p: AppLookupInput) -> ApplicationOutput:
    return _application_out(ctx, _find_application(ctx, p))


class DraftInput(ToolInput):
    job_id: uuid.UUID
    notes: str | None = Field(None, max_length=1000)


class DraftOutput(ToolOutput):
    created: bool
    application_id: str
    application_status: str
    job_status: str
    tailored_resume_version: int | None
    blockers: list[str]  # only the user can resolve these
    pending_steps: list[str]  # PIE/the agent can do these
    note: str


def latest_validated_version(ctx: ToolContext, job: Job) -> ResumeVersion | None:
    return ctx.db.scalar(
        select(ResumeVersion)
        .where(
            ResumeVersion.job_id == job.id,
            ResumeVersion.validation_status == ResumeValidationStatus.VALIDATED,
        )
        .order_by(ResumeVersion.version_number.desc())
        .limit(1)
    )


def create_application_draft(ctx: ToolContext, p: DraftInput) -> DraftOutput:
    job = get_job_or_error(ctx, p.job_id)
    existing = ctx.db.scalar(select(Application).where(Application.job_id == job.id))
    if existing is not None:  # idempotent: one application per job
        readiness = evaluate_readiness(ctx.db, ctx.settings, ctx.profile, job, existing)
        return DraftOutput(
            created=False,
            application_id=str(existing.id),
            application_status=existing.status.value,
            job_status=job.pipeline_state.value,
            tailored_resume_version=_version_number(ctx, existing),
            blockers=readiness.user_blockers,
            pending_steps=readiness.pending_steps,
            note="A draft already exists for this job; nothing new was created.",
        )
    if job.pipeline_state is not S.SHORTLISTED:
        raise ToolError(
            ToolErrorCode.PRECONDITION_FAILED,
            f"Only SHORTLISTED jobs can get an application draft; this job is "
            f"{job.pipeline_state.value}.",
        )
    version = latest_validated_version(ctx, job)
    app = Application(
        job_id=job.id,
        status=ApplicationStatus.DRAFT,
        notes=p.notes,
        resume_version_id=version.id if version else None,
    )
    ctx.db.add(app)
    ctx.db.flush()
    ctx.db.add(
        ApplicationEvent(
            job_id=job.id,
            application_id=app.id,
            event_type="application_draft_created",
            source=EventSource.USER if ctx.actor == "user" else EventSource.AGENT,
            new_status=ApplicationStatus.DRAFT.value,
            reason="Internal draft created. Nothing was sent to the employer.",
        )
    )
    ctx.state_changes.append(
        StateChange("application", str(app.id), None, "DRAFT", "draft created")
    )
    readiness = evaluate_readiness(ctx.db, ctx.settings, ctx.profile, job, app)
    apply_readiness(ctx.db, job, app, readiness, actor=ctx.actor, state_changes=ctx.state_changes)
    return DraftOutput(
        created=True,
        application_id=str(app.id),
        application_status=app.status.value,
        job_status=job.pipeline_state.value,
        tailored_resume_version=version.version_number if version else None,
        blockers=readiness.user_blockers,
        pending_steps=readiness.pending_steps,
        note="Internal draft only. Nothing was submitted or sent.",
    )


# Targets this tool may set. SUBMITTED / APPLICATION_IN_PROGRESS need the browser workflow.
_SETTABLE = {S.READY_TO_APPLY, S.BLOCKED_NEEDS_USER, S.WITHDRAWN}
_NEEDS_SUBMISSION_WORKFLOW = {S.APPLICATION_IN_PROGRESS, S.READY_FOR_SUBMISSION, S.SUBMITTED}


class UpdateStatusInput(ToolInput):
    job_id: uuid.UUID
    target_status: JobPipelineState
    reason: str = Field(min_length=3, max_length=500)
    user_action_required: str | None = Field(None, max_length=1000)


class UpdateStatusOutput(ToolOutput):
    job_id: str
    previous_status: str
    job_status: str
    application_status: str


def update_application_status(ctx: ToolContext, p: UpdateStatusInput) -> UpdateStatusOutput:
    if p.target_status in _NEEDS_SUBMISSION_WORKFLOW:
        raise ToolError(
            ToolErrorCode.PRECONDITION_FAILED,
            f"{p.target_status.value} can only be set by the browser execution tools "
            "(open_application / prepare_submission / a confirmed live submission).",
        )
    if p.target_status not in _SETTABLE:
        raise ToolError(
            ToolErrorCode.INVALID_INPUT,
            f"target_status must be one of {sorted(s.value for s in _SETTABLE)} "
            "(use analyze_job to change analysis states).",
        )
    job = get_job_or_error(ctx, p.job_id)
    app = ctx.db.scalar(select(Application).where(Application.job_id == job.id))
    if app is None:
        raise ToolError(
            ToolErrorCode.PRECONDITION_FAILED, "Create an application draft for this job first."
        )
    previous = job.pipeline_state.value
    if p.target_status is S.READY_TO_APPLY:
        readiness = evaluate_readiness(ctx.db, ctx.settings, ctx.profile, job, app)
        if not readiness.ready:
            missing = readiness.user_blockers + readiness.pending_steps
            raise ToolError(
                ToolErrorCode.PRECONDITION_FAILED,
                "Not ready: " + " | ".join(missing),
                details=readiness.as_dict(),
            )
        apply_readiness(
            ctx.db, job, app, readiness, actor=ctx.actor, state_changes=ctx.state_changes
        )
        return UpdateStatusOutput(
            job_id=str(job.id),
            previous_status=previous,
            job_status=job.pipeline_state.value,
            application_status=app.status.value,
        )
    if p.target_status is S.BLOCKED_NEEDS_USER:
        if not p.user_action_required:
            raise ToolError(
                ToolErrorCode.INVALID_INPUT,
                "user_action_required must say exactly what the user needs to do",
            )
        app.requires_user_input, app.user_action_required = True, p.user_action_required
    elif p.target_status is S.WITHDRAWN:
        app.status = ApplicationStatus.WITHDRAWN
    _move(ctx, job, p.target_status, p.reason, "status_change", app)
    return UpdateStatusOutput(
        job_id=str(job.id),
        previous_status=previous,
        job_status=job.pipeline_state.value,
        application_status=app.status.value,
    )


TOOLS = [
    Tool(
        name="get_application",
        description="Read one application (by application_id or job_id): status, tailored "
        "resume version, readiness (user blockers and pending steps) and timeline.",
        permission=ToolPermission.READ_ONLY,
        input_model=AppLookupInput,
        output_model=ApplicationOutput,
        handler=get_application,
    ),
    Tool(
        name="create_application_draft",
        description="Create an INTERNAL application draft for a SHORTLISTED job (idempotent). "
        "Sends nothing anywhere. Links the job's latest validated tailored resume. The job "
        "becomes READY_TO_APPLY only when nothing is missing; BLOCKED_NEEDS_USER if the user "
        "must provide something; otherwise stays SHORTLISTED with pending steps.",
        permission=ToolPermission.WRITE,
        input_model=DraftInput,
        output_model=DraftOutput,
        handler=create_application_draft,
    ),
    Tool(
        name="update_application_status",
        description="Move a job with a draft to READY_TO_APPLY (only if nothing is missing), "
        "BLOCKED_NEEDS_USER (say what the user must do) or WITHDRAWN. Cannot mark anything "
        "submitted.",
        permission=ToolPermission.WRITE,
        input_model=UpdateStatusInput,
        output_model=UpdateStatusOutput,
        handler=update_application_status,
    ),
]
