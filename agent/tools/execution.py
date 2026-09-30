"""Application execution tools (Phase 5). Claude drives the browser ONLY through these.

None of them accepts a URL, selector, script, file path or free-text value: Claude names a
job, a browser session, a field id PIE detected and an answer KEY. PIE supplies the value
from the verified answer bank, the file from the validated tailored resume, and the URL from
the stored job. Submission needs a LIVE session (EXTERNAL_LIVE_ENABLED=true) and the user's
explicit confirmation, which no tool can give.
"""

import uuid
from typing import Any

from pydantic import Field

from agent.tools.base import (
    Tool,
    ToolContext,
    ToolError,
    ToolErrorCode,
    ToolInput,
    ToolOutput,
)
from backend.services import execution as svc
from database.enums import BrowserExecutionMode, ExecutionMode, ToolPermission
from database.models import Application, BrowserSession


class ExecutionOutput(ToolOutput):
    session_id: str
    application_id: str
    mode: str
    banner: str
    state: str
    job_status: str
    current_step: str | None = None
    current_url: str | None = None
    target_domain: str | None = None
    challenges: list[dict[str, Any]] = []
    missing_answers: list[dict[str, Any]] = []
    blocked_needs_user: bool = False
    user_action: str | None = None
    # step-specific
    reused: bool | None = None
    note: str | None = None
    http_status: int | None = None
    title: str | None = None
    fields_on_page: int | None = None
    failed: bool | None = None
    error: str | None = None
    fields: list[dict[str, Any]] | None = None
    missing_required_fields: list[dict[str, Any]] | None = None
    skipped_fields: list[dict[str, Any]] | None = None
    submit_available: bool | None = None
    verified_answer_keys: list[str] | None = None
    filled: list[str] | None = None
    refused: list[dict[str, Any]] | None = None
    field_errors: list[dict[str, Any]] | None = None
    filled_fields: list[dict[str, Any]] | None = None
    uploaded: dict[str, Any] | None = None
    ready: bool | None = None
    dry_run_complete: bool | None = None
    submit_button: str | None = None
    report: dict[str, Any] | None = None
    submitted: bool | None = None
    outcome: str | None = None
    evidence: dict[str, Any] | None = None
    signals: list[str] | None = None
    resumed: bool | None = None
    page_kept: bool | None = None


def exec_context(ctx: ToolContext, tool: str) -> svc.ExecContext:
    browser = ctx.browser
    if browser is None:
        from integrations.browser import get_browser_manager

        browser = get_browser_manager(ctx.settings)
    live = ctx.execution_mode is ExecutionMode.EXTERNAL_LIVE
    return svc.ExecContext(
        db=ctx.db,
        settings=ctx.settings,
        profile=ctx.profile,
        browser=browser,
        mode=BrowserExecutionMode.LIVE if live else BrowserExecutionMode.DRY_RUN,
        actor=ctx.actor,
        run=ctx.run,
        tool=tool,
        state_changes=ctx.state_changes,
    )


def _call(ctx: ToolContext, tool: str, fn: Any, *args: Any, **kwargs: Any) -> ExecutionOutput:
    ectx = exec_context(ctx, tool)
    try:
        data = fn(ectx, *args, **kwargs)
    except svc.ExecutionError as exc:
        raise ToolError(ToolErrorCode(exc.code), exc.message, details=exc.details) from exc
    return ExecutionOutput(**data)


class OpenInput(ToolInput):
    job_id: uuid.UUID


class SessionInput(ToolInput):
    session_id: uuid.UUID


class InspectInput(SessionInput):
    click_apply: bool = Field(
        False, description="If the page shows an 'Apply' button instead of a form, click it once"
    )


class FieldAssignment(ToolInput):
    field_id: str = Field(max_length=40, pattern=r"^f\d+_\d+$")
    answer_key: str = Field(max_length=120, pattern=r"^[a-z0-9_:]+$")


class FillInput(SessionInput):
    use_mapping: bool = Field(
        True, description="Fill every VERIFIED_AUTOFILL field the deterministic mapping resolved"
    )
    fields: list[FieldAssignment] = Field(
        default_factory=list,
        max_length=40,
        description="Optional subset: [{field_id, answer_key}] with the key the mapping chose",
    )


class UploadInput(SessionInput):
    field_id: str | None = Field(None, max_length=40, pattern=r"^f\d+_\d+$")


class LookupInput(ToolInput):
    session_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None


def open_application(ctx: ToolContext, p: OpenInput) -> ExecutionOutput:
    return _call(ctx, "open_application", svc.open_application, p.job_id)


def inspect_application_form(ctx: ToolContext, p: InspectInput) -> ExecutionOutput:
    return _call(
        ctx, "inspect_application_form", svc.inspect_form, p.session_id, click_apply=p.click_apply
    )


def fill_application_field(ctx: ToolContext, p: FillInput) -> ExecutionOutput:
    pairs = [(f.field_id, f.answer_key) for f in p.fields]
    if not p.use_mapping and not pairs:
        raise ToolError(ToolErrorCode.INVALID_INPUT, "Give fields, or use_mapping=true")
    return _call(
        ctx,
        "fill_application_field",
        svc.fill_fields,
        p.session_id,
        None if p.use_mapping and not pairs else pairs,
    )


def upload_application_resume(ctx: ToolContext, p: UploadInput) -> ExecutionOutput:
    return _call(ctx, "upload_application_resume", svc.upload_resume, p.session_id, p.field_id)


def save_application_progress(ctx: ToolContext, p: SessionInput) -> ExecutionOutput:
    return _call(ctx, "save_application_progress", svc.save_progress, p.session_id)


def prepare_submission(ctx: ToolContext, p: SessionInput) -> ExecutionOutput:
    return _call(ctx, "prepare_submission", svc.prepare_submission, p.session_id)


def submit_application(ctx: ToolContext, p: SessionInput) -> ExecutionOutput:
    return _call(ctx, "submit_application", svc.submit, p.session_id)


def verify_submission(ctx: ToolContext, p: SessionInput) -> ExecutionOutput:
    return _call(ctx, "verify_submission", svc.verify_submission, p.session_id)


def close_application_session(ctx: ToolContext, p: SessionInput) -> ExecutionOutput:
    return _call(ctx, "close_application_session", svc.close_session, p.session_id)


def get_application_session(ctx: ToolContext, p: LookupInput) -> ExecutionOutput:
    if (p.session_id is None) == (p.application_id is None):
        raise ToolError(
            ToolErrorCode.INVALID_INPUT, "Give exactly one of session_id / application_id"
        )
    if p.session_id is not None:
        session = ctx.db.get(BrowserSession, p.session_id)
    else:
        from sqlalchemy import select

        session = ctx.db.scalars(
            select(BrowserSession)
            .where(BrowserSession.application_id == p.application_id)
            .order_by(BrowserSession.created_at.desc())
        ).first()
    if session is None:
        raise ToolError(ToolErrorCode.NOT_FOUND, "No browser session found")
    app = ctx.db.get(Application, session.application_id)
    assert app is not None
    return ExecutionOutput(
        **svc.result(session, app.job),
        fields=session.detected_fields,
        filled_fields=[
            {k: f[k] for k in ("field_id", "label", "key")} for f in session.filled_fields or []
        ],
        skipped_fields=session.skipped_fields,
        uploaded=session.uploaded_resume,
        evidence=session.submission_evidence,
        report=svc.dry_run_report(session),
    )


def _tool(
    name: str,
    description: str,
    permission: ToolPermission,
    model: type[ToolInput],
    handler: Any,
) -> Tool:
    return Tool(
        name=name,
        description=description,
        permission=permission,
        input_model=model,
        output_model=ExecutionOutput,
        handler=handler,
        manages_transaction=True,  # commits its own audit trail before/after browser actions
    )


EXT, WRITE, READ = ToolPermission.EXTERNAL_ACTION, ToolPermission.WRITE, ToolPermission.READ_ONLY
TOOLS = [
    _tool(
        "open_application",
        "Open the stored application page of a READY_TO_APPLY job in a browser session "
        "(dry run unless the run is external_live). Checks the draft, the validated tailored "
        "resume and duplicates first; stops on CAPTCHA/OTP/login. Resumes a paused session.",
        EXT,
        OpenInput,
        open_application,
    ),
    _tool(
        "inspect_application_form",
        "Detect the form fields and map each to a VERIFIED answer key. Returns fields, "
        "missing_required_fields, challenges and submit_available. Unknown required questions "
        "pause for the user (BLOCKED_NEEDS_USER) - never guess.",
        EXT,
        InspectInput,
        inspect_application_form,
    ),
    _tool(
        "fill_application_field",
        "Fill fields with VERIFIED answers. use_mapping=true fills everything the mapping "
        "resolved; or give [{field_id, answer_key}] (only for unrecognised text fields and "
        "contact keys). You never supply values; PIE does.",
        EXT,
        FillInput,
        fill_application_field,
    ),
    _tool(
        "upload_application_resume",
        "Upload the application's validated tailored resume PDF into the resume field.",
        EXT,
        UploadInput,
        upload_application_resume,
    ),
    _tool(
        "save_application_progress",
        "Record the session's current state and a screenshot in PIE (nothing is clicked).",
        WRITE,
        SessionInput,
        save_application_progress,
    ),
    _tool(
        "prepare_submission",
        "Validate the filled form (required fields, errors, resume, one submit button) and "
        "stop before the final submit. In a dry run this is the last step: nothing is "
        "submitted.",
        EXT,
        SessionInput,
        prepare_submission,
    ),
    _tool(
        "submit_application",
        "IRREVERSIBLE. Click the final submit - only in a LIVE session, only after the USER "
        "confirmed this submission in the dashboard, never twice. Dry runs are refused. "
        "SUBMITTED requires positive confirmation on the page.",
        EXT,
        SessionInput,
        submit_application,
    ),
    _tool(
        "verify_submission",
        "Re-check the page for positive confirmation of a live submission attempt.",
        EXT,
        SessionInput,
        verify_submission,
    ),
    _tool(
        "close_application_session",
        "Close the browser session. If nothing was submitted the job returns to READY_TO_APPLY.",
        WRITE,
        SessionInput,
        close_application_session,
    ),
    _tool(
        "get_application_session",
        "Stored state of a browser session (fields, filled, missing answers, challenges, "
        "screenshots, evidence).",
        READ,
        LookupInput,
        get_application_session,
    ),
]
