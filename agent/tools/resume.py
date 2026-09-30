"""Resume tools. The master resume is read-only; tailored versions are new internal artifacts."""

import uuid
from typing import Any

from pydantic import Field
from sqlalchemy import select

from agent.tailoring.schema import RequestedFormat, TailoringStrategy
from agent.tools.base import (
    NoInput,
    Tool,
    ToolContext,
    ToolError,
    ToolErrorCode,
    ToolInput,
    ToolOutput,
)
from backend.services.resumes import ResumeUnavailableError, read_master_resume
from database.enums import ToolPermission
from database.models import ResumeVersion


class MasterResumeOutput(ToolOutput):
    file_name: str
    source: str
    sha256: str
    text: str


def get_master_resume(ctx: ToolContext, _: NoInput) -> MasterResumeOutput:
    try:
        resume = read_master_resume(ctx.settings)
    except ResumeUnavailableError as exc:
        raise ToolError(ToolErrorCode.NOT_FOUND, str(exc)) from exc
    return MasterResumeOutput(
        file_name=resume.path.name, source=resume.source, sha256=resume.sha256, text=resume.text
    )


class VersionOut(ToolOutput):
    id: str
    version_number: int
    is_master: bool
    validation_status: str | None
    file_path: str
    job_id: str | None
    company: str | None
    role: str | None
    tailoring_strategy: str | None
    created_at: str
    changes_summary: str | None


def version_out(v: ResumeVersion) -> VersionOut:
    return VersionOut(
        id=str(v.id),
        version_number=v.version_number,
        is_master=v.is_master,
        validation_status=v.validation_status.value if v.validation_status else None,
        file_path=v.file_path,
        job_id=str(v.job_id) if v.job_id else None,
        company=v.company,
        role=v.role,
        tailoring_strategy=v.tailoring_strategy,
        created_at=v.created_at.isoformat(),
        changes_summary=v.changes_summary,
    )


class VersionsInput(ToolInput):
    job_id: uuid.UUID | None = None


class VersionsOutput(ToolOutput):
    versions: list[VersionOut]


def list_resume_versions(ctx: ToolContext, p: VersionsInput) -> VersionsOutput:
    stmt = select(ResumeVersion).order_by(ResumeVersion.created_at)
    if p.job_id is not None:
        stmt = stmt.where(ResumeVersion.job_id == p.job_id)
    return VersionsOutput(versions=[version_out(v) for v in ctx.db.scalars(stmt).all()])


class VersionIdInput(ToolInput):
    resume_version_id: uuid.UUID


class VersionDetailOutput(ToolOutput):
    version: VersionOut
    content: dict[str, Any] | None
    change_details: dict[str, Any] | None
    tailoring_report: dict[str, Any] | None
    validation_report: dict[str, Any] | None


def get_resume_version(ctx: ToolContext, p: VersionIdInput) -> VersionDetailOutput:
    v = ctx.db.get(ResumeVersion, p.resume_version_id)
    if v is None:
        raise ToolError(ToolErrorCode.NOT_FOUND, f"No resume version {p.resume_version_id}")
    return VersionDetailOutput(
        version=version_out(v),
        content=v.content,
        change_details=v.change_details,
        tailoring_report=v.tailoring_report,
        validation_report=v.validation_report,
    )


class TailorInput(ToolInput):
    job_id: uuid.UUID
    application_id: uuid.UUID | None = None
    tailoring_strategy: TailoringStrategy = "balanced"
    requested_format: RequestedFormat = "pdf"
    force_new: bool = Field(
        False, description="Create a new version even if an identical one already exists"
    )


class TailorOutput(ToolOutput):
    resume_version_id: str
    version_number: int
    created: bool
    validation_status: str
    artifact_path: str
    attempts: int
    change_summary: dict[str, Any] | None
    tailoring_report: dict[str, Any] | None
    readiness: dict[str, Any] | None
    note: str


def create_tailored_resume(ctx: ToolContext, p: TailorInput) -> TailorOutput:
    from backend.services.tailoring import TailoringError, tailor_resume
    from integrations.job_sources.config import load_job_sources_config

    if ctx.claude_factory is None:
        raise ToolError(ToolErrorCode.EXECUTION_FAILED, "Claude is not available for tailoring")
    vocabulary = load_job_sources_config(ctx.settings.job_sources_path).skills_vocabulary
    try:
        result = tailor_resume(
            ctx.db,
            settings=ctx.settings,
            profile=ctx.profile,
            claude=ctx.claude_factory(),
            job_id=p.job_id,
            application_id=p.application_id,
            strategy=p.tailoring_strategy,
            requested_format=p.requested_format,
            force_new=p.force_new,
            vocabulary=vocabulary,
            actor=ctx.actor,
            state_changes=ctx.state_changes,
        )
    except TailoringError as exc:
        raise ToolError(
            ToolErrorCode(exc.code), exc.message, details={"violations": exc.violations}
        ) from exc
    v = result.version
    return TailorOutput(
        resume_version_id=str(v.id),
        version_number=v.version_number,
        created=result.created,
        validation_status=v.validation_status.value if v.validation_status else "UNKNOWN",
        artifact_path=v.file_path,
        attempts=result.attempts,
        change_summary=v.change_details,
        tailoring_report=v.tailoring_report,
        readiness=result.readiness.as_dict() if result.readiness else None,
        note="Internal artifact only. Nothing was submitted or sent. The master resume is "
        "unchanged."
        if result.created
        else "An identical validated version already exists; it was reused.",
    )


TOOLS = [
    Tool(
        name="get_master_resume",
        description="Read the text of the user's master resume (the source of truth). Read-only.",
        permission=ToolPermission.READ_ONLY,
        input_model=NoInput,
        output_model=MasterResumeOutput,
        handler=get_master_resume,
    ),
    Tool(
        name="list_resume_versions",
        description="List stored resume versions (master and tailored), optionally for one job.",
        permission=ToolPermission.READ_ONLY,
        input_model=VersionsInput,
        output_model=VersionsOutput,
        handler=list_resume_versions,
    ),
    Tool(
        name="get_resume_version",
        description="One resume version: structured content, change summary, tailoring report "
        "and validation report.",
        permission=ToolPermission.READ_ONLY,
        input_model=VersionIdInput,
        output_model=VersionDetailOutput,
        handler=get_resume_version,
    ),
    Tool(
        name="create_tailored_resume",
        description="Create a NEW job-specific resume version from the immutable master resume "
        "(Claude tailors, PIE validates every claim; unsupported claims are rejected). Internal "
        "artifact only - nothing is submitted. Links to the job's application draft if one "
        "exists and re-evaluates readiness. Slow (about 30-60s).",
        permission=ToolPermission.WRITE,
        input_model=TailorInput,
        output_model=TailorOutput,
        handler=create_tailored_resume,
        manages_transaction=True,
    ),
]
