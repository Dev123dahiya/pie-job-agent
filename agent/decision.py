"""The agent's per-step decision: exactly one of call a tool, finish, or ask the user.

Claude returns this as structured JSON (API structured outputs or CLI --json-schema). It is
validated here; anything malformed is rejected and re-asked, never guessed at.
"""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Action = Literal["call_tool", "complete", "need_user_input"]


class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=300)  # one line, shown in the dashboard
    action: Action
    tool: str = Field(default="", max_length=128)
    arguments_json: str = Field(default="{}", max_length=20000)
    message: str = Field(default="", max_length=4000)  # final result / what the user must do

    @model_validator(mode="after")
    def _consistent(self) -> "AgentDecision":
        if self.action == "call_tool":
            if not self.tool.strip():
                raise ValueError("action 'call_tool' needs a tool name")
            self.arguments()  # must parse
        elif not self.message.strip():
            raise ValueError(f"action '{self.action}' needs a message for the user")
        return self

    def arguments(self) -> dict[str, Any]:
        try:
            value = json.loads(self.arguments_json or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"arguments_json is not valid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError("arguments_json must encode a JSON object")
        return value


class DecisionError(ValueError):
    """Claude's reply was not a valid decision."""


DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "One short line: what you are doing now."},
        "action": {"type": "string", "enum": ["call_tool", "complete", "need_user_input"]},
        "tool": {"type": "string", "description": "Tool name for call_tool, else empty."},
        "arguments_json": {
            "type": "string",
            "description": 'JSON object with the tool\'s arguments, e.g. \'{"job_id": "..."}\'.',
        },
        "message": {
            "type": "string",
            "description": "For complete: the result for the user. For need_user_input: exactly "
            "what the user must provide or do. Empty for call_tool.",
        },
    },
    "required": ["summary", "action", "tool", "arguments_json", "message"],
    "additionalProperties": False,
}


def parse_decision(payload: str | dict[str, Any]) -> AgentDecision:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise DecisionError(f"Reply is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DecisionError("Reply must be a JSON object")
    try:
        return AgentDecision.model_validate(payload)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}" for e in exc.errors()
        )
        raise DecisionError(f"Invalid decision: {problems}") from exc
