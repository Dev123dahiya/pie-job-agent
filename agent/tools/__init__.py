"""The agent's tool registry. Claude acts only through these tools."""

from agent.tools import (
    agent_state,
    application,
    execution,
    external,
    jobs,
    profile,
    reporting,
    resume,
)
from agent.tools.base import (
    EXTERNAL_MODES,
    Tool,
    ToolContext,
    ToolError,
    ToolErrorCode,
    ToolRegistry,
    ToolResult,
)


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for module in (profile, jobs, resume, application, agent_state, reporting, execution, external):
        for tool in module.TOOLS:
            registry.register(tool)
    return registry


__all__ = [
    "EXTERNAL_MODES",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolErrorCode",
    "ToolRegistry",
    "ToolResult",
    "build_default_registry",
]
