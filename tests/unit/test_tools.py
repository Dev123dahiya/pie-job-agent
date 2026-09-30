"""Tool registry: schemas, validation, permission gates and honest behaviour of each tool."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent.agent_prompt import build_agent_message
from agent.tools import ToolContext, build_default_registry
from agent.tools.base import NoInput, Tool, ToolErrorCode, ToolOutput, ToolRegistry
from backend.core.config import Settings
from backend.core.profile import UserProfile
from database.enums import ToolPermission
from database.models import Application, Company, ResumeVersion

RW = frozenset({ToolPermission.READ_ONLY, ToolPermission.WRITE})
RO = frozenset({ToolPermission.READ_ONLY})

REQUIRED_TOOLS = {
    "get_user_profile",
    "get_application_answers",
    "search_jobs",
    "get_job",
    "analyze_job",
    "get_shortlisted_jobs",
    "get_master_resume",
    "list_resume_versions",
    "create_tailored_resume",
    "get_application",
    "create_application_draft",
    "update_application_status",
    "get_agent_status",
    "get_current_run",
    "record_agent_action",
    "get_application_statistics",
    "export_application_data",
}


@pytest.fixture
def ctx(db: Session, agent_settings: Settings, profile: UserProfile) -> ToolContext:
    return ToolContext(db=db, settings=agent_settings, profile=profile, allowed=RW)


def run(ctx: ToolContext, name: str, **args: Any):  # type: ignore[no-untyped-def]
    return build_default_registry().execute(name, args, ctx)


def test_every_required_tool_is_registered_with_full_metadata() -> None:
    registry = build_default_registry()
    names = {t.name for t in registry.tools()}
    assert names >= REQUIRED_TOOLS
    for tool in registry.tools():
        assert tool.description and tool.input_schema()["type"] == "object"
        assert tool.output_schema()["type"] == "object"
        assert isinstance(tool.permission, ToolPermission)
    catalog = {c["name"]: c for c in registry.catalog()}
    assert catalog["get_job"]["permission"] == "READ_ONLY"
    assert catalog["create_application_draft"]["permission"] == "WRITE"


def test_permission_classification() -> None:
    by_name = {t.name: t.permission for t in build_default_registry().tools()}
    assert by_name["get_user_profile"] is ToolPermission.READ_ONLY
    assert by_name["get_application_statistics"] is ToolPermission.READ_ONLY
    for write in (
        "create_application_draft",
        "update_application_status",
        "record_agent_action",
        "create_tailored_resume",
        "analyze_job",
        "export_application_data",
    ):
        assert by_name[write] is ToolPermission.WRITE, write
    for ext in (
        "open_application", "inspect_application_form", "fill_application_field",
        "upload_application_resume", "prepare_submission", "submit_application",
        "verify_submission", "send_email",
    ):  # fmt: skip
        assert by_name[ext] is ToolPermission.EXTERNAL_ACTION, ext
    assert by_name["save_application_progress"] is ToolPermission.WRITE
    assert by_name["close_application_session"] is ToolPermission.WRITE
    assert by_name["get_application_session"] is ToolPermission.READ_ONLY
    assert "open_external_website" not in by_name  # no arbitrary website tool exists


def test_duplicate_registration_rejected() -> None:
    registry = build_default_registry()
    with pytest.raises(ValueError, match="already registered"):
        registry.register(registry.tools()[0])


def test_schema_validation(ctx: ToolContext) -> None:
    assert run(ctx, "nope").error_code is ToolErrorCode.UNKNOWN_TOOL
    bad = run(ctx, "get_job", job_id="not-a-uuid")
    assert bad.error_code is ToolErrorCode.INVALID_INPUT and "job_id" in (bad.message or "")
    extra = run(ctx, "search_jobs", limit=5, shell="rm -rf /")
    assert extra.error_code is ToolErrorCode.INVALID_INPUT and "shell" in (extra.message or "")
    assert run(ctx, "search_jobs", limit=500).error_code is ToolErrorCode.INVALID_INPUT
    reg = build_default_registry()
    assert reg.execute("search_jobs", "{not json", ctx).error_code is ToolErrorCode.INVALID_INPUT


def test_external_actions_need_an_external_mode(ctx: ToolContext) -> None:
    from database.enums import ExecutionMode

    calls: list[str] = []

    def handler(c: ToolContext, p: NoInput) -> ToolOutput:
        calls.append("ran")
        raise AssertionError("external handler must never execute")

    registry = ToolRegistry()
    registry.register(
        Tool("fake_submit", "x", ToolPermission.EXTERNAL_ACTION, NoInput, ToolOutput, handler)
    )
    ctx.allowed = frozenset(ToolPermission)  # every permission, but an internal mode
    result = registry.execute("fake_submit", {}, ctx)
    assert result.error_code is ToolErrorCode.EXTERNAL_NOT_ALLOWED and calls == []
    ctx.execution_mode = ExecutionMode.EXTERNAL_LIVE  # live requested
    assert ctx.settings.development_mode is True and ctx.settings.external_live_enabled is False
    result = registry.execute("fake_submit", {}, ctx)
    assert result.error_code is ToolErrorCode.DEVELOPMENT_MODE_BLOCKED and calls == []
    # Even with EXTERNAL_LIVE_ENABLED=true, DEVELOPMENT_MODE=true still blocks live.
    ctx.settings = ctx.settings.model_copy(update={"external_live_enabled": True})
    result = registry.execute("fake_submit", {}, ctx)
    assert result.error_code is ToolErrorCode.DEVELOPMENT_MODE_BLOCKED and calls == []
    # Production mode without the live flag: LIVE_NOT_ENABLED.
    ctx.settings = ctx.settings.model_copy(
        update={"development_mode": False, "external_live_enabled": False}
    )
    result = registry.execute("fake_submit", {}, ctx)
    assert result.error_code is ToolErrorCode.LIVE_NOT_ENABLED and calls == []
    ctx.execution_mode = ExecutionMode.INTERNAL_WRITES
    session = "00000000-0000-0000-0000-000000000000"
    for name, args in (
        ("submit_application", {"session_id": session}),
        ("open_application", {"job_id": session}),
    ):
        assert run(ctx, name, **args).error_code is ToolErrorCode.EXTERNAL_NOT_ALLOWED
    email = run(ctx, "send_email", to="a@b.c", subject="s", body="b")
    assert email.error_code is ToolErrorCode.NOT_ENABLED


def test_write_tools_denied_in_read_only_mode(ctx: ToolContext, db: Session) -> None:
    ctx.allowed = RO
    result = run(ctx, "record_agent_action", summary="note")
    assert result.error_code is ToolErrorCode.PERMISSION_DENIED
    assert run(ctx, "get_application_statistics").ok


def test_failed_tool_rolls_back_its_writes(ctx: ToolContext, db: Session) -> None:
    from agent.tools.base import ToolError

    def handler(c: ToolContext, p: NoInput) -> ToolOutput:
        c.db.add(Company(name="Half Written", normalized_name="half written"))
        c.db.flush()
        raise ToolError(ToolErrorCode.EXECUTION_FAILED, "boom")

    registry = ToolRegistry()
    registry.register(Tool("partial", "x", ToolPermission.WRITE, NoInput, ToolOutput, handler))
    assert not registry.execute("partial", {}, ctx).ok
    assert db.scalar(select(func.count()).select_from(Company)) == 0


def test_profile_tool_never_verifies_unconfirmed_skills(ctx: ToolContext) -> None:
    ctx.profile.verified_profile.skills.append("Go")
    ctx.profile.verified_profile.unconfirmed_skills = ["Go"]
    data = run(ctx, "get_user_profile").data or {}
    assert "Go" not in data["verified_profile"]["skills"]
    assert "unconfirmed_skills" not in data["verified_profile"]
    assert data["unconfirmed_skills"] == ["Go"]
    assert "email" not in data["verified_profile"] and "phone" not in data["verified_profile"]
    message = build_agent_message(task="t", profile=ctx.profile, state={}, tools=[], history=[])
    verified_section = message.split("VERIFIED_PROFILE:\n")[1].split("\n\nUNCONFIRMED:")[0]
    assert '"Go"' not in verified_section


def test_application_answers_report_missing_instead_of_guessing(ctx: ToolContext) -> None:
    ctx.profile.verified_profile.verified_answers = {"notice_period": "Immediate"}
    data = run(ctx, "get_application_answers").data or {}
    assert data["answers"]["notice_period"] == "Immediate"
    assert data["answers"]["email"] == "candidate@example.com"
    missing = {m["key"] for m in data["missing"]}
    assert "work_authorization" in missing and "notice_period" not in missing


def test_tailoring_without_claude_creates_nothing(ctx: ToolContext, db: Session) -> None:
    result = run(ctx, "create_tailored_resume", job_id="00000000-0000-0000-0000-000000000001")
    assert result.error_code is ToolErrorCode.EXECUTION_FAILED
    assert "Claude is not available" in (result.message or "")
    assert db.scalar(select(func.count()).select_from(ResumeVersion)) == 0


def test_master_resume_is_read_only(ctx: ToolContext, tmp_path: Path) -> None:
    assert run(ctx, "get_master_resume").error_code is ToolErrorCode.NOT_FOUND
    resume = ctx.settings.resumes_dir / "master" / "resume.md"
    resume.write_text("# Test Candidate\nPython developer", encoding="utf-8")
    before = resume.read_bytes()
    data = run(ctx, "get_master_resume").data or {}
    assert data["text"].startswith("# Test Candidate") and data["source"] == "resumes/master"
    assert resume.read_bytes() == before  # untouched


def test_statistics_and_export(ctx: ToolContext, seeded: None) -> None:
    stats = run(ctx, "get_application_statistics").data or {}
    assert stats["jobs_total"] == 7 and stats["submitted_applications"] == 0
    export = run(ctx, "export_application_data", format="csv").data or {}
    path = Path(export["path"])
    assert path.parent == ctx.settings.export_dir and export["rows"] == 7
    assert path.read_text(encoding="utf-8").startswith("company,title")


def test_search_and_get_job(ctx: ToolContext, seeded: None) -> None:
    found = run(ctx, "search_jobs", query="nimbus").data or {}
    assert found["total"] == 1
    job_id = found["jobs"][0]["id"]
    detail = run(ctx, "get_job", job_id=job_id).data or {}
    assert detail["job"]["title"] == "Backend Software Engineer" and detail["application"] is None
    assert run(ctx, "get_job", job_id="00000000-0000-0000-0000-000000000009").error_code is (
        ToolErrorCode.NOT_FOUND
    )


def test_application_draft_flow(ctx: ToolContext, db: Session, shortlisted_job_id: Any) -> None:
    jid = str(shortlisted_job_id)
    first = run(ctx, "create_application_draft", job_id=jid)
    assert first.ok and first.data and first.data["created"] is True
    # No master resume + missing answers -> blocked with the exact reasons, never guessed.
    assert first.data["job_status"] == "BLOCKED_NEEDS_USER"
    assert any("Master resume" in b for b in first.data["blockers"])
    assert any("work_authorization" in b for b in first.data["blockers"])
    job_changes = [c for c in first.state_changes if c["entity"] == "job"]
    assert [(c["previous"], c["new"]) for c in job_changes] == [
        ("SHORTLISTED", "BLOCKED_NEEDS_USER")
    ]
    again = run(ctx, "create_application_draft", job_id=jid)
    assert again.ok and again.data and again.data["created"] is False  # idempotent
    assert db.scalar(select(func.count()).select_from(Application)) == 1
    app = db.scalar(select(Application))
    assert app is not None and app.status.value == "DRAFT" and app.applied_at is None


def test_draft_waits_for_tailored_resume_when_answers_complete(
    ctx: ToolContext, shortlisted_job_id: Any
) -> None:
    (ctx.settings.resumes_dir / "master" / "resume.md").write_text("resume", encoding="utf-8")
    ctx.profile.verified_profile.verified_answers = {
        "work_authorization": "Yes",
        "notice_period": "Immediate",
        "expected_salary": "10 LPA",
        "willing_to_relocate": "No",
    }
    result = run(ctx, "create_application_draft", job_id=str(shortlisted_job_id))
    # Nothing user-only is missing, but there is no validated tailored resume yet:
    # the job stays SHORTLISTED with a pending step (not blocked on the user, not ready).
    assert result.ok and result.data
    assert result.data["job_status"] == "SHORTLISTED" and result.data["blockers"] == []
    assert any("tailored resume" in s for s in result.data["pending_steps"])


def test_drafts_only_for_shortlisted_jobs(ctx: ToolContext, seeded: None) -> None:
    job_id = (run(ctx, "search_jobs", query="quantum").data or {})["jobs"][0]["id"]
    result = run(ctx, "create_application_draft", job_id=job_id)
    assert result.error_code is ToolErrorCode.PRECONDITION_FAILED


def test_status_updates_cannot_submit(ctx: ToolContext, shortlisted_job_id: Any) -> None:
    jid = str(shortlisted_job_id)
    run(ctx, "create_application_draft", job_id=jid)
    for target in ("SUBMITTED", "APPLICATION_IN_PROGRESS", "READY_FOR_SUBMISSION"):
        r = run(ctx, "update_application_status", job_id=jid, target_status=target, reason="try")
        assert r.error_code is ToolErrorCode.PRECONDITION_FAILED
    not_ready = run(
        ctx,
        "update_application_status",
        job_id=jid,
        target_status="READY_TO_APPLY",
        reason="ready?",
    )
    assert not_ready.error_code is ToolErrorCode.PRECONDITION_FAILED
    blocked = run(
        ctx,
        "update_application_status",
        job_id=jid,
        target_status="BLOCKED_NEEDS_USER",
        reason="needs info",
    )
    assert blocked.error_code is ToolErrorCode.INVALID_INPUT  # must say what the user must do
    withdrawn = run(
        ctx,
        "update_application_status",
        job_id=jid,
        target_status="WITHDRAWN",
        reason="not interested",
    )
    assert withdrawn.ok
    illegal = run(
        ctx,
        "update_application_status",
        job_id=jid,
        target_status="BLOCKED_NEEDS_USER",
        reason="again",
        user_action_required="x",
    )
    assert illegal.error_code is ToolErrorCode.INVALID_TRANSITION
