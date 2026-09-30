"""PIE Agent Orchestrator: runs Claude as a tool-using agent under PIE's control.

Loop (one iteration = one decision):
    load profile -> build state -> ask Claude for ONE decision -> validate it ->
    permission-checked tool call through the registry -> record events -> repeat
until the task is complete, needs the user, is cancelled, hits its iteration/time budget or
fails. Claude never executes anything itself: it can only name a registered tool.

All state lives in the database (AgentRun + AgentAction events), so a run survives dashboard
refreshes and can be resumed: history is rebuilt from the stored events.
"""

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agent.agent_prompt import AGENT_PROMPT_VERSION, AGENT_SYSTEM_PROMPT, build_agent_message
from agent.claude_client import ClaudeCallError, ClaudeUnavailableError, StructuredClaude
from agent.decision import DECISION_SCHEMA, AgentDecision, DecisionError, parse_decision
from agent.tools import ToolContext, ToolRegistry, ToolResult, build_default_registry
from agent.tools.base import ToolErrorCode
from backend.core.config import Settings, get_settings, live_execution_block
from backend.core.profile import UserProfile, load_user_profile
from backend.services.runs import RunLogger, cancel_requested, ensure_no_active_run, finish_run
from database.base import utcnow
from database.enums import (
    AgentActionStatus,
    AgentEvent,
    AgentRunStatus,
    ExecutionMode,
    RunType,
    ToolPermission,
)
from database.models import AgentAction, AgentRun

logger = logging.getLogger(__name__)

_EXTERNAL = frozenset(
    {ToolPermission.READ_ONLY, ToolPermission.WRITE, ToolPermission.EXTERNAL_ACTION}
)
MODES: dict[str, frozenset[ToolPermission]] = {
    "read_only": frozenset({ToolPermission.READ_ONLY}),
    "internal_writes": frozenset({ToolPermission.READ_ONLY, ToolPermission.WRITE}),
    # Browser execution. Dry run is the default external mode; live must be enabled explicitly
    # (EXTERNAL_LIVE_ENABLED=true) and still needs the user's per-application confirmation.
    "external_dry_run": _EXTERNAL,
    "external_live": _EXTERNAL,
}


class LiveModeDisabledError(ValueError):
    """external_live was requested but EXTERNAL_LIVE_ENABLED is not true."""


def check_mode(mode: str, live_enabled: bool, development_mode: bool = True) -> None:
    """external_live needs DEVELOPMENT_MODE=false AND EXTERNAL_LIVE_ENABLED=true."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    if mode != ExecutionMode.EXTERNAL_LIVE.value:
        return
    if development_mode:
        raise LiveModeDisabledError(
            "DEVELOPMENT_MODE_BLOCKED: DEVELOPMENT_MODE is true, so external_live is blocked "
            "regardless of EXTERNAL_LIVE_ENABLED."
        )
    if not live_enabled:
        raise LiveModeDisabledError(
            "LIVE_NOT_ENABLED: set EXTERNAL_LIVE_ENABLED=true to allow external_live. PIE never "
            "switches a run to live on its own."
        )


MAX_ITERATIONS_CAP = 40
RESUMABLE = {AgentRunStatus.NEEDS_USER_INPUT, AgentRunStatus.FAILED, AgentRunStatus.STOPPED}
STORED_RESULT_CHARS = 20_000


@dataclass(frozen=True)
class AgentLimits:
    decision_retries: int = 2  # re-asks after a malformed decision
    claude_retries: int = 2  # retries after a transient Claude error
    tool_retries: int = 1  # retries of a failed READ_ONLY tool marked retryable
    max_duration_seconds: float = 1800
    history_steps: int = 16  # most recent steps sent back to Claude in full
    history_result_chars: int = 4000
    repeat_warning_at: int = 2  # identical calls before a warning instead of execution
    repeat_fail_at: int = 4  # identical calls before the run stops as looping
    backoff_seconds: float = 2.0


class AgentStepError(RuntimeError):
    pass


def idempotency_key(tool: str, arguments: dict[str, Any]) -> str:
    canonical = json.dumps({"tool": tool, "args": arguments}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _stored(result: ToolResult) -> dict[str, Any]:
    data = result.model_dump(mode="json", exclude_none=True)
    text = json.dumps(data, ensure_ascii=False)
    if len(text) <= STORED_RESULT_CHARS:
        return data
    return {"ok": result.ok, "tool": result.tool, "truncated": text[:STORED_RESULT_CHARS]}


# --- Run lifecycle ------------------------------------------------------------------------


def start_agent_run(
    db: Session,
    task: str,
    *,
    mode: str = "internal_writes",
    max_iterations: int = 12,
    dev_mode: bool = True,
    backend: str | None = None,
    live_enabled: bool = False,
    development_mode: bool = True,
) -> AgentRun:
    check_mode(mode, live_enabled, development_mode)
    if not 1 <= max_iterations <= MAX_ITERATIONS_CAP:
        raise ValueError(f"max_iterations must be between 1 and {MAX_ITERATIONS_CAP}")
    ensure_no_active_run(db)  # one active run at a time (discovery, analysis or agent)
    run = AgentRun(
        task=task.strip(),
        run_type=RunType.AGENT,
        status=AgentRunStatus.RUNNING,
        development_mode=dev_mode,
        mode=mode,
        iterations=0,
        max_iterations=max_iterations,
        searches_total=max_iterations,  # progress denominator shown by the dashboard
        current_task="Starting",
        heartbeat_at=utcnow(),
        model=backend,
    )
    db.add(run)
    db.flush()
    RunLogger(db, run).log(
        AgentEvent.RUN_STARTED,
        input={
            "task": run.task,
            "mode": mode,
            "max_iterations": max_iterations,
            "prompt_version": AGENT_PROMPT_VERSION,
        },
    )
    db.commit()
    return run


def resume_agent_run(
    db: Session,
    run_id: uuid.UUID,
    *,
    message: str | None = None,
    extra_iterations: int = 0,
    live_enabled: bool = False,
    development_mode: bool = True,
) -> AgentRun:
    run = db.get(AgentRun, run_id)
    if run is None or run.run_type is not RunType.AGENT:
        raise LookupError("Agent run not found")
    check_mode(run.mode or "internal_writes", live_enabled, development_mode)
    if run.status not in RESUMABLE:
        raise ValueError(f"Run is {run.status.value}; only paused, failed or stopped runs resume")
    ensure_no_active_run(db)
    if extra_iterations:
        run.max_iterations = min(MAX_ITERATIONS_CAP, (run.max_iterations or 0) + extra_iterations)
        run.searches_total = run.max_iterations
    if (run.max_iterations or 0) <= run.iterations:
        raise ValueError("Iteration budget exhausted; pass extra_iterations to continue")
    log = RunLogger(db, run)
    log.log(AgentEvent.RUN_RESUMED, input={"previous_status": run.status.value})
    if message:
        log.log(AgentEvent.USER_MESSAGE, input={"message": message}, reasoning=message[:300])
    run.status = AgentRunStatus.RUNNING
    run.pending_user_input = None
    run.ended_at = None
    run.heartbeat_at = utcnow()
    run.current_task = "Resuming"
    db.commit()
    return run


# --- Execution ----------------------------------------------------------------------------


class AgentRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        claude: StructuredClaude,
        *,
        registry: ToolRegistry | None = None,
        settings: Settings | None = None,
        browser: Any = None,
        profile_loader: Callable[[], UserProfile] = load_user_profile,
        analysis_context_builder: Callable[[UserProfile], Any] | None = None,
        limits: AgentLimits | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session_factory = session_factory
        self.claude = claude
        self.registry = registry or build_default_registry()
        self.settings = settings or get_settings()
        self.browser = browser  # None: the process-wide BrowserManager, created on first use
        self.profile_loader = profile_loader
        self.analysis_context_builder = analysis_context_builder
        self.limits = limits or AgentLimits()
        self.sleep = sleep
        self.clock = clock

    def execute(self, run_id: uuid.UUID) -> AgentRun:
        with self.session_factory() as db:
            run = db.get(AgentRun, run_id)
            if run is None:
                raise ValueError(f"Run {run_id} not found")
            log = RunLogger(db, run)
            try:
                self._loop(db, run, log)
            except Exception as exc:  # the run must never be left RUNNING
                logger.exception("Agent run %s crashed", run_id)
                db.rollback()
                run = db.get(AgentRun, run_id) or run
                log = RunLogger(db, run)
                self._fail(db, run, log, f"Unexpected error: {type(exc).__name__}: {exc}")
            return run

    # -- internals --

    def _fail(self, db: Session, run: AgentRun, log: RunLogger, reason: str) -> None:
        log.error(reason)
        log.log(AgentEvent.RUN_FAILED, status=AgentActionStatus.FAILED, error=reason)
        finish_run(run, AgentRunStatus.FAILED, summary=reason)
        db.commit()

    def _loop(self, db: Session, run: AgentRun, log: RunLogger) -> None:
        try:
            profile = self.profile_loader()
        except Exception as exc:
            self._fail(db, run, log, f"Could not load the user profile: {exc}")
            return
        run.model = self.claude.model or self.claude.name
        started = self.clock()
        allowed = MODES.get(run.mode or "internal_writes", MODES["read_only"])
        block = live_execution_block(self.settings)
        if run.mode == ExecutionMode.EXTERNAL_LIVE.value and block is not None:
            self._fail(db, run, log, f"{block[0]}: {block[1]}")
            return

        while True:
            if cancel_requested(db, run):
                log.log(AgentEvent.RUN_CANCELLED, output={"at_iteration": run.iterations})
                finish_run(run, AgentRunStatus.STOPPED, summary="Cancelled by the user")
                db.commit()
                return
            if run.iterations >= (run.max_iterations or 0):
                self._fail(db, run, log, f"Iteration limit reached ({run.max_iterations})")
                return
            if self.clock() - started > self.limits.max_duration_seconds:
                self._fail(db, run, log, "Time limit reached")
                return

            run.iterations += 1
            run.searches_completed = run.iterations
            log.heartbeat(f"Step {run.iterations}: deciding next action")
            db.commit()

            history, calls = self._history(db, run)
            try:
                decision = self._decide(db, run, log, profile, allowed, history)
            except ClaudeUnavailableError as exc:
                self._fail(db, run, log, f"Claude is unavailable: {exc}")
                return
            except AgentStepError as exc:
                self._fail(db, run, log, str(exc))
                return

            if decision.action == "complete":
                log.log(
                    AgentEvent.RUN_COMPLETED,
                    reasoning=decision.summary,
                    output={"message": decision.message},
                )
                finish_run(run, AgentRunStatus.COMPLETED, summary=decision.message)
                db.commit()
                return
            if decision.action == "need_user_input":
                log.log(
                    AgentEvent.USER_INPUT_REQUIRED,
                    reasoning=decision.summary,
                    output={"message": decision.message},
                )
                run.pending_user_input = decision.message
                finish_run(run, AgentRunStatus.NEEDS_USER_INPUT, summary=decision.summary)
                db.commit()
                return

            if cancel_requested(db, run):  # never start a tool after a stop request
                continue
            if self._call_tool(db, run, log, profile, allowed, decision, calls):
                return  # run ended (looping)

    def _decide(
        self,
        db: Session,
        run: AgentRun,
        log: RunLogger,
        profile: UserProfile,
        allowed: frozenset[ToolPermission],
        history: list[dict[str, Any]],
    ) -> AgentDecision:
        state = {
            "run_id": str(run.id),
            "iteration": run.iterations,
            "max_iterations": run.max_iterations,
            "mode": run.mode,
            "allowed_permissions": sorted(p.value for p in allowed),
            "execution_mode": run.mode,
            "external_live_enabled": live_execution_block(self.settings) is None,
            "development_mode": self.settings.development_mode,
            "dry_run": run.mode == ExecutionMode.EXTERNAL_DRY_RUN.value,
        }
        feedback: str | None = None
        malformed = transient = 0
        while True:
            message = build_agent_message(
                task=run.task,
                profile=profile,
                state=state,
                tools=self._tool_catalog(),
                history=history,
                feedback=feedback,
            )
            try:
                reply = self.claude.call(AGENT_SYSTEM_PROMPT, message, DECISION_SCHEMA)
            except ClaudeUnavailableError:
                raise
            except ClaudeCallError as exc:
                transient += 1
                log.log(
                    AgentEvent.THINKING,
                    status=AgentActionStatus.FAILED,
                    error=f"Claude call failed (attempt {transient}): {exc}",
                )
                db.commit()
                if transient > self.limits.claude_retries:
                    raise AgentStepError(f"Claude failed {transient} times: {exc}") from exc
                self.sleep(self.limits.backoff_seconds * transient)
                continue
            try:
                decision = parse_decision(reply.payload)
            except DecisionError as exc:
                malformed += 1
                log.log(
                    AgentEvent.THINKING,
                    status=AgentActionStatus.FAILED,
                    error=f"Malformed decision rejected (attempt {malformed}): {exc}",
                )
                db.commit()
                if malformed > self.limits.decision_retries:
                    raise AgentStepError(
                        f"Claude returned {malformed} malformed decisions; stopping"
                    ) from exc
                feedback = str(exc)
                continue
            log.log(
                AgentEvent.THINKING,
                reasoning=decision.summary,
                input={"iteration": run.iterations},
                output={
                    "action": decision.action,
                    "tool": decision.tool or None,
                    "arguments_json": decision.arguments_json if decision.tool else None,
                },
            ).iteration = run.iterations
            run.current_task = decision.summary
            db.commit()
            return decision

    def _tool_catalog(self) -> list[dict[str, Any]]:
        catalog = []
        for entry in self.registry.catalog():
            schema = {k: v for k, v in entry["input_schema"].items() if k != "title"}
            catalog.append({**entry, "input_schema": schema})
        return catalog

    def _call_tool(
        self,
        db: Session,
        run: AgentRun,
        log: RunLogger,
        profile: UserProfile,
        allowed: frozenset[ToolPermission],
        decision: AgentDecision,
        calls: dict[str, list[AgentAction]],
    ) -> bool:
        """Execute one tool call. Returns True if the run has ended."""
        args = decision.arguments()
        key = idempotency_key(decision.tool, args)
        tool = self.registry.get(decision.tool)
        prior = calls.get(key, [])

        def record(event: AgentEvent, result: ToolResult, status: AgentActionStatus) -> None:
            entry = log.log(
                event,
                input={"tool": decision.tool, "arguments": args},
                output=_stored(result),
                status=status,
                error=None if result.ok else f"{result.error_code}: {result.message}",
                reasoning=decision.summary,
            )
            entry.tool, entry.iteration, entry.idempotency_key = decision.tool, run.iterations, key

        # Duplicate protection: an identical write that already succeeded is not repeated.
        succeeded = [a for a in prior if a.status is AgentActionStatus.SUCCESS]
        if tool is not None and tool.permission is ToolPermission.WRITE and succeeded:
            result = ToolResult(
                ok=True,
                tool=decision.tool,
                data={"previous_result": (succeeded[-1].output or {}).get("data")},
                message="Duplicate write skipped: this exact call already succeeded in this run.",
            )
            record(AgentEvent.TOOL_CALL_COMPLETED, result, AgentActionStatus.SKIPPED)
            db.commit()
            return False
        if len(prior) >= self.limits.repeat_fail_at:
            self._fail(
                db,
                run,
                log,
                f"Agent repeated the same {decision.tool} call "
                f"{len(prior) + 1} times; stopped to avoid a loop",
            )
            return True
        if len(prior) >= self.limits.repeat_warning_at:
            result = ToolResult(
                ok=False,
                tool=decision.tool,
                error_code=ToolErrorCode.PRECONDITION_FAILED,
                message=f"You already made this exact call {len(prior)} times. Its result is in "
                "HISTORY. Change approach or finish.",
            )
            record(AgentEvent.TOOL_CALL_FAILED, result, AgentActionStatus.SKIPPED)
            db.commit()
            return False

        started = log.log(
            AgentEvent.TOOL_CALL_STARTED,
            input={"tool": decision.tool, "arguments": args},
            reasoning=decision.summary,
        )
        started.tool, started.iteration, started.idempotency_key = (
            decision.tool,
            run.iterations,
            key,
        )
        log.heartbeat(f"Step {run.iterations}: {decision.tool}")
        db.commit()

        builder = self.analysis_context_builder
        ctx = ToolContext(
            db=db,
            settings=self.settings,
            profile=profile,
            allowed=allowed,
            actor="agent",
            run=run,
            log=log,
            analysis_context_factory=(lambda: builder(profile)) if builder else None,
            claude_factory=lambda: self.claude,
            execution_mode=ExecutionMode(run.mode or "internal_writes"),
            browser=self.browser,
        )
        attempts = 0
        while True:
            result = self.registry.execute(decision.tool, args, ctx)
            retry_ok = (
                not result.ok
                and result.retryable
                and tool is not None
                and tool.permission is ToolPermission.READ_ONLY
                and attempts < self.limits.tool_retries
            )
            if not retry_ok:
                break
            attempts += 1
            self.sleep(self.limits.backoff_seconds)

        record(
            AgentEvent.TOOL_CALL_COMPLETED if result.ok else AgentEvent.TOOL_CALL_FAILED,
            result,
            AgentActionStatus.SUCCESS if result.ok else AgentActionStatus.FAILED,
        )
        for change in result.state_changes:
            log.log(
                AgentEvent.STATE_CHANGED,
                input={"tool": decision.tool},
                output=change,
                reasoning=f"{change['entity']} {change['previous']} -> {change['new']}",
            )
        log.heartbeat()
        db.commit()
        # Deterministic pause: a browser step that needs the user (CAPTCHA, OTP, login, unknown
        # question, unconfirmed submission) stops the run here - Claude cannot talk past it.
        if result.ok and (result.data or {}).get("blocked_needs_user"):
            message = (result.data or {}).get("user_action") or "The application needs you"
            log.log(
                AgentEvent.USER_INPUT_REQUIRED,
                reasoning="Browser execution paused for the user",
                output={"message": message, "session_id": (result.data or {}).get("session_id")},
            )
            run.pending_user_input = message
            finish_run(run, AgentRunStatus.NEEDS_USER_INPUT, summary=message[:500])
            db.commit()
            return True
        return False

    def _history(
        self, db: Session, run: AgentRun
    ) -> tuple[list[dict[str, Any]], dict[str, list[AgentAction]]]:
        """Rebuild the run's history from stored events (this is what makes runs resumable)."""
        actions = db.scalars(
            select(AgentAction).where(AgentAction.run_id == run.id).order_by(AgentAction.sequence)
        ).all()
        steps: list[dict[str, Any]] = []
        calls: dict[str, list[AgentAction]] = {}
        for a in actions:
            if a.tool_name == AgentEvent.USER_MESSAGE.value:
                steps.append({"user_message": (a.input or {}).get("message")})
            elif a.tool_name in (
                AgentEvent.TOOL_CALL_COMPLETED.value,
                AgentEvent.TOOL_CALL_FAILED.value,
            ):
                if a.idempotency_key:
                    calls.setdefault(a.idempotency_key, []).append(a)
                result = json.dumps(a.output or {}, ensure_ascii=False)
                limit = self.limits.history_result_chars
                steps.append(
                    {
                        "step": a.iteration,
                        "summary": a.reasoning,
                        "tool": a.tool,
                        "arguments": (a.input or {}).get("arguments"),
                        "result": result
                        if len(result) <= limit
                        else result[:limit] + "...(truncated)",
                    }
                )
            elif a.tool_name == AgentEvent.USER_INPUT_REQUIRED.value:
                steps.append({"asked_user": (a.output or {}).get("message")})
        if len(steps) > self.limits.history_steps:
            older = len(steps) - self.limits.history_steps
            steps = [
                {"note": f"{older} earlier steps omitted"},
                *steps[-self.limits.history_steps :],
            ]
        return steps, calls
