"""Tool abstraction for the autonomous agent.

Claude can only affect the world through tools registered here. Every call is:

1. looked up by name              (unknown tool -> UNKNOWN_TOOL)
2. validated against its schema   (bad/extra arguments -> INVALID_INPUT)
3. permission-checked             (disabled tool -> NOT_ENABLED; permission outside the run's
                                    mode -> PERMISSION_DENIED; EXTERNAL_ACTION only in an
                                    external mode; LIVE only when EXTERNAL_LIVE_ENABLED=true)
4. executed in a DB savepoint     (any failure rolls the tool's writes back)
5. output-validated and returned as a structured ToolResult (never an exception)
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.orm import Session

from backend.core.config import Settings, live_execution_block
from backend.core.profile import UserProfile
from database.enums import ExecutionMode, ExternalActionResult, ToolPermission
from database.models import AgentRun, ExternalActionLog
from database.state_machine import InvalidTransitionError

logger = logging.getLogger(__name__)

EXTERNAL_MODES = {ExecutionMode.EXTERNAL_DRY_RUN, ExecutionMode.EXTERNAL_LIVE}


class ToolErrorCode(StrEnum):
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    INVALID_INPUT = "INVALID_INPUT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_ENABLED = "NOT_ENABLED"  # the tool is switched off entirely (e.g. send_email)
    EXTERNAL_NOT_ALLOWED = "EXTERNAL_NOT_ALLOWED"  # external tool outside an external mode
    LIVE_NOT_ENABLED = "LIVE_NOT_ENABLED"  # live mode requested; EXTERNAL_LIVE_ENABLED is false
    DEVELOPMENT_MODE_BLOCKED = "DEVELOPMENT_MODE_BLOCKED"  # live requested with DEVELOPMENT_MODE
    VISIBLE_BROWSER_REQUIRED = "VISIBLE_BROWSER_REQUIRED"  # live needs BROWSER_HEADLESS=false
    DRY_RUN_ONLY = "DRY_RUN_ONLY"  # the irreversible step was asked for in a dry run
    USER_CONFIRMATION_REQUIRED = "USER_CONFIRMATION_REQUIRED"
    DUPLICATE_APPLICATION = "DUPLICATE_APPLICATION"
    DAILY_LIMIT_REACHED = "DAILY_LIMIT_REACHED"  # a live submission over today's limit
    MODE_MISMATCH = "MODE_MISMATCH"
    UNEXPECTED_PAGE = "UNEXPECTED_PAGE"
    BROWSER_ERROR = "BROWSER_ERROR"
    NOT_IMPLEMENTED_YET = "NOT_IMPLEMENTED_YET"
    NOT_FOUND = "NOT_FOUND"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    VALIDATION_FAILED = "VALIDATION_FAILED"


class ToolError(Exception):
    """Raised by tool handlers for expected failures; becomes a failed ToolResult."""

    def __init__(
        self,
        code: ToolErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


class ToolInput(BaseModel):
    """Base for tool inputs: unknown arguments are an error, never silently ignored."""

    model_config = ConfigDict(extra="forbid")


class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoInput(ToolInput):
    pass


@dataclass
class StateChange:
    entity: str  # job | application
    entity_id: str
    previous: str | None
    new: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ToolContext:
    db: Session
    settings: Settings
    profile: UserProfile
    allowed: frozenset[ToolPermission]
    actor: str = "agent"  # agent | user
    run: AgentRun | None = None
    # Optional services, injected by the orchestrator/API (kept lazy: may call Claude).
    log: Any = None  # backend.services.runs.RunLogger
    analysis_context_factory: Callable[[], Any] | None = None
    # Claude for tools that need it (resume tailoring). None: such tools report unavailable.
    claude_factory: Callable[[], Any] | None = None
    # What external work is allowed. Only EXTERNAL_DRY_RUN / EXTERNAL_LIVE run EXTERNAL_ACTION
    # tools; READ_ONLY / INTERNAL_WRITES never touch a website.
    execution_mode: ExecutionMode = ExecutionMode.INTERNAL_WRITES
    browser: Any = None  # integrations.browser.BrowserManager (lazy for tests)
    state_changes: list[StateChange] = field(default_factory=list)


class ToolResult(BaseModel):
    ok: bool
    tool: str
    data: dict[str, Any] | None = None
    error_code: ToolErrorCode | None = None
    message: str | None = None
    retryable: bool = False
    state_changes: list[dict[str, Any]] = []

    def for_model(self, max_chars: int = 6000) -> str:
        """Compact JSON for the agent's history, truncated to keep prompts bounded."""
        text = json.dumps(self.model_dump(mode="json", exclude_none=True), ensure_ascii=False)
        return text if len(text) <= max_chars else text[:max_chars] + "...(truncated)"


Handler = Callable[[ToolContext, Any], ToolOutput]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    permission: ToolPermission
    input_model: type[ToolInput]
    output_model: type[ToolOutput]
    handler: Handler
    # True for tools that commit their own work (e.g. analyze_job, which calls Claude and
    # records several steps). Everything else runs inside a savepoint owned by the registry.
    manages_transaction: bool = False
    # False: registered so the agent knows it exists, but it never runs (e.g. send_email).
    available: bool = True

    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def output_schema(self) -> dict[str, Any]:
        return self.output_model.model_json_schema()

    @property
    def enabled(self) -> bool:
        return self.available


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def catalog(self) -> list[dict[str, Any]]:
        """What the agent (and the dashboard) sees about each tool."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "permission": t.permission.value,
                "enabled": t.enabled,
                "input_schema": t.input_schema(),
            }
            for t in self._tools.values()
        ]

    def execute(
        self, name: str, arguments: dict[str, Any] | str | None, ctx: ToolContext
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return _fail(
                name,
                ToolErrorCode.UNKNOWN_TOOL,
                f"No tool named {name!r}. Use one of: {', '.join(sorted(self._tools))}",
            )

        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError as exc:
                return _fail(
                    name, ToolErrorCode.INVALID_INPUT, f"Arguments are not valid JSON: {exc}"
                )
        if not isinstance(arguments, dict):
            return _fail(name, ToolErrorCode.INVALID_INPUT, "Arguments must be a JSON object")
        try:
            params = tool.input_model.model_validate(arguments)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}" for e in exc.errors()
            )
            return _fail(name, ToolErrorCode.INVALID_INPUT, f"Invalid arguments: {problems}")

        # Permission gate - checked before the handler can possibly run.
        refusal = _gate(tool, ctx)
        if refusal is not None:
            if tool.permission is ToolPermission.EXTERNAL_ACTION:
                _log_refusal(ctx, name, refusal)
            return refusal

        ctx.state_changes = []
        try:
            if tool.manages_transaction:
                output = tool.handler(ctx, params)
            else:
                with ctx.db.begin_nested():
                    output = tool.handler(ctx, params)
                    output = tool.output_model.model_validate(output.model_dump())
                ctx.db.commit()
        except ToolError as exc:
            if tool.manages_transaction:
                ctx.db.rollback()
            return _fail(name, exc.code, exc.message, retryable=exc.retryable, details=exc.details)
        except InvalidTransitionError as exc:
            return _fail(name, ToolErrorCode.INVALID_TRANSITION, str(exc))
        except Exception as exc:  # a tool bug must not take the run (or the DB) down
            logger.exception("Tool %s failed", name)
            if tool.manages_transaction:
                ctx.db.rollback()
            return _fail(
                name,
                ToolErrorCode.EXECUTION_FAILED,
                f"{type(exc).__name__}: {exc}",
                retryable=tool.permission is ToolPermission.READ_ONLY,
            )
        return ToolResult(
            ok=True,
            tool=name,
            data=output.model_dump(mode="json"),
            state_changes=[c.as_dict() for c in ctx.state_changes],
        )


def _fail(
    name: str,
    code: ToolErrorCode,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> ToolResult:
    data = {"details": details} if details else None
    return ToolResult(
        ok=False, tool=name, error_code=code, message=message, retryable=retryable, data=data
    )


def _gate(tool: Tool, ctx: ToolContext) -> ToolResult | None:
    name = tool.name
    if not tool.enabled:
        return _fail(name, ToolErrorCode.NOT_ENABLED, f"{name} is not enabled. Nothing was done.")
    if tool.permission not in ctx.allowed:
        return _fail(
            name,
            ToolErrorCode.PERMISSION_DENIED,
            f"{name} needs {tool.permission.value} permission, which this run does not "
            f"have (allowed: {', '.join(sorted(p.value for p in ctx.allowed))}).",
        )
    if tool.permission is ToolPermission.EXTERNAL_ACTION:
        if ctx.execution_mode not in EXTERNAL_MODES:
            return _fail(
                name,
                ToolErrorCode.EXTERNAL_NOT_ALLOWED,
                f"{name} touches an external website and needs an external execution mode "
                f"(this is {ctx.execution_mode.value}). Nothing was done.",
            )
        if ctx.execution_mode is ExecutionMode.EXTERNAL_LIVE:
            block = live_execution_block(ctx.settings)
            if block is not None:
                return _fail(name, ToolErrorCode(block[0]), block[1])
    return None


def _log_refusal(ctx: ToolContext, name: str, refusal: ToolResult) -> None:
    """Refused external actions are audited too (nothing external happened)."""
    try:
        ctx.db.add(
            ExternalActionLog(
                run_id=ctx.run.id if ctx.run else None,
                tool=name,
                mode=ctx.execution_mode.value,
                actor=ctx.actor,
                summary=f"Refused before running: {refusal.message}",
                result=ExternalActionResult.REFUSED,
                error=str(refusal.error_code),
            )
        )
        ctx.db.commit()
    except Exception:  # auditing must never break the gate itself
        logger.exception("Could not record refused external action")
        ctx.db.rollback()
