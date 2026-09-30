"""The autonomous agent loop, end to end on the mock job database with a scripted Claude."""

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from agent.analysis.analyzers import HeuristicAnalyzer
from agent.claude_client import ClaudeCallError, ClaudeUnavailableError
from backend.core.config import Settings
from backend.core.profile import UserProfile
from backend.services.agent_runner import (
    AgentLimits,
    AgentRunner,
    resume_agent_run,
    start_agent_run,
)
from backend.services.analysis import AnalysisContext
from backend.services.runs import RunConflictError, request_cancel
from backend.services.shortlist import ShortlistRules
from database.enums import AgentRunStatus
from database.models import AgentAction, AgentRun, Application, Job
from integrations.job_sources.config import JobSourcesConfig
from tests.conftest import FakeClaude, decision


@pytest.fixture
def make_runner(
    session_factory: sessionmaker[Session],
    agent_settings: Settings,
    profile: UserProfile,
    sources_config: JobSourcesConfig,
):  # type: ignore[no-untyped-def]
    def build(claude: FakeClaude, **limits: Any) -> AgentRunner:
        analyzer = HeuristicAnalyzer(
            sources_config.search.location_patterns, sources_config.search.term_aliases
        )
        return AgentRunner(
            session_factory,
            claude,
            settings=agent_settings,
            profile_loader=lambda: profile,
            analysis_context_builder=lambda p: AnalysisContext(
                analyzer=analyzer, profile=p, rules=ShortlistRules(), replace_outdated=True
            ),
            limits=AgentLimits(backoff_seconds=0, **limits),
            sleep=lambda s: None,
        )

    return build


def start(db: Session, task: str = "Review my pipeline", **kw: Any) -> AgentRun:
    return start_agent_run(db, task, **kw)


def fresh(db: Session, run_id: Any) -> AgentRun:
    """Re-read a run: the runner works in its own session, so drop cached state first."""
    db.expire_all()
    run = db.get(AgentRun, run_id)
    assert run is not None
    return run


def events(db: Session, run_id: Any) -> list[str]:
    rows = db.scalars(
        select(AgentAction).where(AgentAction.run_id == run_id).order_by(AgentAction.sequence)
    ).all()
    return [a.tool_name for a in rows]


def tool_results(db: Session, run_id: Any) -> list[AgentAction]:
    return list(
        db.scalars(
            select(AgentAction)
            .where(
                AgentAction.run_id == run_id,
                AgentAction.tool_name.in_(["TOOL_CALL_COMPLETED", "TOOL_CALL_FAILED"]),
            )
            .order_by(AgentAction.sequence)
        ).all()
    )


@pytest.mark.usefixtures("seeded")
def test_run_lifecycle_and_structured_events(db: Session, make_runner) -> None:  # type: ignore[no-untyped-def]
    claude = FakeClaude(
        decision(tool="get_user_profile", summary="Read the profile"),
        decision(tool="search_jobs", summary="Find Nimbus", query="nimbus"),
        decision(action="complete", message="Found 1 Nimbus job.", summary="Done"),
    )
    run = start(db)
    assert run.status is AgentRunStatus.RUNNING and run.run_type.value == "agent"
    result = make_runner(claude).execute(run.id)
    db.expire_all()
    run = fresh(db, run.id)
    assert run is not None and run.status is AgentRunStatus.COMPLETED
    assert run.summary == "Found 1 Nimbus job." and run.iterations == 3
    assert run.model == "fake-claude" and result.id == run.id
    assert events(db, run.id) == [
        "RUN_STARTED",
        "THINKING",
        "TOOL_CALL_STARTED",
        "TOOL_CALL_COMPLETED",
        "THINKING",
        "TOOL_CALL_STARTED",
        "TOOL_CALL_COMPLETED",
        "THINKING",
        "RUN_COMPLETED",
    ]
    search = tool_results(db, run.id)[1]
    assert search.tool == "search_jobs" and search.iteration == 2
    assert (search.output or {})["data"]["total"] == 1
    # Only concise summaries are stored as "reasoning" - no hidden chain of thought.
    thinking = db.scalars(select(AgentAction).where(AgentAction.tool_name == "THINKING")).all()
    assert [t.reasoning for t in thinking] == ["Read the profile", "Find Nimbus", "Done"]


@pytest.mark.usefixtures("seeded")
def test_claude_receives_required_context(db: Session, make_runner, profile: UserProfile) -> None:  # type: ignore[no-untyped-def]
    profile.verified_profile.skills.append("Go")
    profile.verified_profile.unconfirmed_skills = ["Go"]
    claude = FakeClaude(decision(action="complete", message="ok"))
    run = start(db)
    make_runner(claude).execute(run.id)
    prompt = claude.prompts[0]
    for section in (
        "TASK:",
        "VERIFIED_PROFILE:",
        "PREFERENCES:",
        "CURRENT_AGENT_STATE:",
        "AVAILABLE_TOOLS:",
        "HISTORY:",
    ):
        assert section in prompt, section
    verified = prompt.split("VERIFIED_PROFILE:\n")[1].split("\n\nUNCONFIRMED:")[0]
    assert '"Go"' not in verified and "candidate@example.com" not in verified
    assert '"external_live_enabled":false' in prompt


@pytest.mark.usefixtures("seeded")
def test_tool_errors_are_fed_back_and_run_continues(db: Session, make_runner) -> None:  # type: ignore[no-untyped-def]
    claude = FakeClaude(
        decision(tool="delete_everything"),
        decision(tool="get_job", job_id="not-a-uuid"),
        decision(tool="send_email", to="a@b.c", subject="s", body="b"),
        decision(action="complete", message="Stopped trying."),
    )
    run = start(db)
    make_runner(claude).execute(run.id)
    codes = [(a.output or {}).get("error_code") for a in tool_results(db, run.id)]
    assert codes == ["UNKNOWN_TOOL", "INVALID_INPUT", "NOT_ENABLED"]
    assert "NOT_ENABLED" in claude.prompts[3]  # Claude saw the refusal
    assert fresh(db, run.id).status is AgentRunStatus.COMPLETED


@pytest.mark.usefixtures("seeded")
def test_read_only_mode_blocks_writes(db: Session, make_runner, shortlisted_job_id: Any) -> None:  # type: ignore[no-untyped-def]
    claude = FakeClaude(
        decision(tool="create_application_draft", job_id=str(shortlisted_job_id)),
        decision(action="complete", message="Could not draft in read-only mode."),
    )
    run = start(db, mode="read_only")
    make_runner(claude).execute(run.id)
    assert (tool_results(db, run.id)[0].output or {})["error_code"] == "PERMISSION_DENIED"
    assert db.scalar(select(func.count()).select_from(Application)) == 0


def test_malformed_decisions_are_retried_then_fail(db: Session, make_runner) -> None:  # type: ignore[no-untyped-def]
    ok = start(db)
    make_runner(
        FakeClaude("not json", {"action": "complete"}, decision(action="complete", message="fine"))
    ).execute(ok.id)
    assert fresh(db, ok.id).status is AgentRunStatus.COMPLETED

    bad = start(db)
    make_runner(FakeClaude("x", "y", "z")).execute(bad.id)
    run = fresh(db, bad.id)
    assert run is not None and run.status is AgentRunStatus.FAILED
    assert "malformed" in (run.summary or "")
    assert events(db, bad.id)[-1] == "RUN_FAILED"


def test_transient_claude_errors_are_retried(db: Session, make_runner) -> None:  # type: ignore[no-untyped-def]
    run = start(db)
    make_runner(
        FakeClaude(
            ClaudeCallError("timeout"),
            ClaudeCallError("timeout"),
            decision(action="complete", message="ok"),
        )
    ).execute(run.id)
    assert fresh(db, run.id).status is AgentRunStatus.COMPLETED

    down = start(db)
    make_runner(FakeClaude(ClaudeUnavailableError("not signed in"))).execute(down.id)
    failed = fresh(db, down.id)
    assert failed is not None and failed.status is AgentRunStatus.FAILED
    assert "unavailable" in (failed.summary or "")


@pytest.mark.usefixtures("seeded")
def test_duplicate_writes_are_not_repeated(
    db: Session, make_runner, shortlisted_job_id: Any
) -> None:  # type: ignore[no-untyped-def]
    draft = decision(tool="create_application_draft", job_id=str(shortlisted_job_id))
    run = start(db)
    make_runner(FakeClaude(draft, draft, decision(action="complete", message="ok"))).execute(run.id)
    results = tool_results(db, run.id)
    assert [r.status.value for r in results] == ["SUCCESS", "SKIPPED"]
    assert "Duplicate write skipped" in ((results[1].output or {}).get("message") or "")
    assert db.scalar(select(func.count()).select_from(Application)) == 1


@pytest.mark.usefixtures("seeded")
def test_repeated_identical_calls_stop_the_loop(db: Session, make_runner) -> None:  # type: ignore[no-untyped-def]
    same = decision(tool="get_application_statistics")
    run = start(db, max_iterations=10)
    make_runner(FakeClaude(*[same] * 6)).execute(run.id)
    final = fresh(db, run.id)
    assert final is not None and final.status is AgentRunStatus.FAILED
    assert "avoid a loop" in (final.summary or "")
    statuses = [r.status.value for r in tool_results(db, run.id)]
    assert statuses == ["SUCCESS", "SUCCESS", "SKIPPED", "SKIPPED"]


@pytest.mark.usefixtures("seeded")
def test_user_input_required_then_resume(db: Session, make_runner) -> None:  # type: ignore[no-untyped-def]
    run = start(db)
    make_runner(
        FakeClaude(
            decision(
                action="need_user_input",
                summary="Need auth answer",
                message="Are you authorized to work in India?",
            )
        )
    ).execute(run.id)
    paused = fresh(db, run.id)
    assert paused is not None and paused.status is AgentRunStatus.NEEDS_USER_INPUT
    assert paused.pending_user_input == "Are you authorized to work in India?"
    start(db, task="another task")  # a paused run does not block new runs...
    other = db.scalar(select(AgentRun).where(AgentRun.task == "another task"))
    request_cancel(db, other.id)
    make_runner(FakeClaude()).execute(other.id)

    resume_agent_run(db, run.id, message="Yes, I am authorized to work in India.")
    claude = FakeClaude(decision(action="complete", message="Noted."))
    make_runner(claude).execute(run.id)
    assert "Yes, I am authorized to work in India." in claude.prompts[0]
    done = fresh(db, run.id)
    assert done is not None and done.status is AgentRunStatus.COMPLETED
    assert done.pending_user_input is None
    assert "USER_INPUT_REQUIRED" in events(db, run.id) and "RUN_RESUMED" in events(db, run.id)


def test_cancellation(db: Session, make_runner) -> None:  # type: ignore[no-untyped-def]
    run = start(db)
    request_cancel(db, run.id)
    make_runner(FakeClaude(decision(tool="get_user_profile"))).execute(run.id)
    stopped = fresh(db, run.id)
    assert stopped is not None and stopped.status is AgentRunStatus.STOPPED
    assert events(db, run.id)[-1] == "RUN_CANCELLED"

    paused = start(db)
    make_runner(FakeClaude(decision(action="need_user_input", message="?"))).execute(paused.id)
    request_cancel(db, paused.id)
    assert fresh(db, paused.id).status is AgentRunStatus.STOPPED


@pytest.mark.usefixtures("seeded")
def test_cancel_mid_run_stops_before_the_next_tool(
    db: Session, make_runner, session_factory: sessionmaker[Session]
) -> None:  # type: ignore[no-untyped-def]
    run = start(db)

    def cancel_then_decide(_: str) -> dict[str, Any]:
        with session_factory() as other:
            request_cancel(other, run.id)
        return decision(tool="get_application_statistics")

    make_runner(FakeClaude(cancel_then_decide)).execute(run.id)
    assert fresh(db, run.id).status is AgentRunStatus.STOPPED
    assert tool_results(db, run.id) == []  # the tool never ran


@pytest.mark.usefixtures("seeded")
def test_iteration_limit(db: Session, make_runner) -> None:  # type: ignore[no-untyped-def]
    run = start(db, max_iterations=2)
    make_runner(
        FakeClaude(decision(tool="get_user_profile"), decision(tool="get_application_statistics"))
    ).execute(run.id)
    final = fresh(db, run.id)
    assert final is not None and final.status is AgentRunStatus.FAILED
    assert "Iteration limit" in (final.summary or "") and final.iterations == 2
    with pytest.raises(ValueError, match="budget"):
        resume_agent_run(db, run.id)
    resume_agent_run(db, run.id, extra_iterations=1)  # more budget -> resumable


def test_only_one_active_run(db: Session) -> None:
    start(db)
    with pytest.raises(RunConflictError):
        start(db, task="second")
    with pytest.raises(ValueError):
        start_agent_run(db, "bad mode", mode="yolo")


@pytest.mark.usefixtures("seeded")
def test_crash_is_recorded_and_run_resumes_from_persisted_history(db: Session, make_runner) -> None:  # type: ignore[no-untyped-def]
    run = start(db)
    make_runner(
        FakeClaude(decision(tool="search_jobs", query="orbit"), RuntimeError("worker crashed"))
    ).execute(run.id)
    crashed = fresh(db, run.id)
    assert crashed is not None and crashed.status is AgentRunStatus.FAILED
    assert "worker crashed" in (crashed.summary or "")

    resume_agent_run(db, run.id)
    claude = FakeClaude(decision(action="complete", message="Recovered."))
    make_runner(claude).execute(run.id)
    history = claude.prompts[0].split("HISTORY:\n")[1]
    assert '"tool":"search_jobs"' in history  # rebuilt from the database, not memory
    assert fresh(db, run.id).status is AgentRunStatus.COMPLETED


@pytest.mark.usefixtures("seeded")
def test_state_changes_are_recorded(db: Session, make_runner) -> None:  # type: ignore[no-untyped-def]
    job = db.scalar(select(Job).where(Job.source_job_id == "mock-001"))
    assert job is not None
    run = start(db)
    make_runner(
        FakeClaude(
            decision(tool="analyze_job", job_id=str(job.id)),
            decision(tool="create_application_draft", job_id=str(job.id)),
            decision(action="complete", message="Drafted."),
        )
    ).execute(run.id)
    changes = db.scalars(
        select(AgentAction)
        .where(AgentAction.run_id == run.id, AgentAction.tool_name == "STATE_CHANGED")
        .order_by(AgentAction.sequence)
    ).all()
    transitions = [((c.output or {})["previous"], (c.output or {})["new"]) for c in changes]
    assert ("DISCOVERED", "SHORTLISTED") in transitions
    assert ("SHORTLISTED", "BLOCKED_NEEDS_USER") in transitions
    db.expire_all()
    assert db.get(Job, job.id).pipeline_state.value == "BLOCKED_NEEDS_USER"
    app = db.scalar(select(Application))
    assert app is not None and app.status.value == "DRAFT" and app.applied_at is None
