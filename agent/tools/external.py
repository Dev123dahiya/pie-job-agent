"""External tools that stay switched off. Email automation is a later phase: send_email is
registered so the agent knows it exists, but the registry refuses it (NOT_ENABLED) before any
handler runs. Arbitrary website access is not a tool at all - see agent/tools/execution.py."""

from pydantic import Field

from agent.tools.base import Tool, ToolContext, ToolError, ToolErrorCode, ToolInput, ToolOutput
from database.enums import ToolPermission


class EmailInput(ToolInput):
    to: str = Field(max_length=320)
    subject: str = Field(max_length=300)
    body: str = Field(max_length=10000)


class ExternalOutput(ToolOutput):
    done: bool


def _never(ctx: ToolContext, _: ToolInput) -> ExternalOutput:
    # Defence in depth: unreachable while the tool is unavailable.
    raise ToolError(ToolErrorCode.NOT_ENABLED, "Sending email is not enabled.")


TOOLS = [
    Tool(
        name="send_email",
        description="NOT ENABLED (email automation is a later phase). Would send an email.",
        permission=ToolPermission.EXTERNAL_ACTION,
        input_model=EmailInput,
        output_model=ExternalOutput,
        handler=_never,
        available=False,
    ),
]
