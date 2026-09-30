"""One structured Claude call: system + user text in, schema-shaped JSON out.

Shared by the job analyzer and the autonomous agent, so there is one code path per
backend:

- API: Anthropic SDK, structured outputs (output_config.format), server-side refusal
  fallback. Needs ANTHROPIC_API_KEY.
- CLI: the local Claude Code CLI in print mode with --json-schema, using the user's own
  Claude subscription login. Runs with NO tools, NO MCP servers and NO settings/hooks, in a
  neutral working directory: Claude can only answer, never act.
"""

import json
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Protocol

from anthropic.types.beta import BetaMessageParam, BetaOutputConfigParam


class ClaudeCallError(RuntimeError):
    """The call failed (network, refusal, timeout, bad output...). Usually retryable."""


class ClaudeUnavailableError(ClaudeCallError):
    """No usable backend (bad key, CLI missing). Not retryable."""


@dataclass
class StructuredReply:
    payload: str | dict[str, Any]  # JSON text (API) or parsed object (CLI)
    model: str | None


class StructuredClaude(Protocol):
    name: str
    model: str | None

    def call(self, system: str, user: str, schema: dict[str, Any]) -> StructuredReply: ...


class ApiStructuredClaude:
    name = "claude_api"

    def __init__(self, client: Any, model: str, effort: str) -> None:
        import anthropic

        self._anthropic = anthropic
        self.client = client
        self.model: str | None = model
        self.effort = effort

    @classmethod
    def from_key(
        cls, api_key: str, model: str, effort: str, timeout: float
    ) -> "ApiStructuredClaude":
        import anthropic

        return cls(
            anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2), model, effort
        )

    def call(self, system: str, user: str, schema: dict[str, Any]) -> StructuredReply:
        anthropic = self._anthropic
        messages: list[BetaMessageParam] = [{"role": "user", "content": user}]
        output_config: BetaOutputConfigParam = {
            "effort": self.effort,  # type: ignore[typeddict-item]
            "format": {"type": "json_schema", "schema": schema},
        }
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=messages,
                output_config=output_config,
                # Server-side refusal fallback, routed by refusal category.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as exc:
            raise ClaudeUnavailableError("Anthropic API key was rejected") from exc
        except anthropic.RateLimitError as exc:
            raise ClaudeCallError("Anthropic API rate limit reached; try again later") from exc
        except anthropic.APIStatusError as exc:
            raise ClaudeCallError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ClaudeCallError("Could not reach the Anthropic API") from exc

        if response.stop_reason == "refusal":
            raise ClaudeCallError("Claude declined the request")
        if response.stop_reason == "max_tokens":
            raise ClaudeCallError("Claude's answer was cut off (max_tokens)")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise ClaudeCallError("Claude returned no text output")
        return StructuredReply(payload=text, model=response.model)


class CliStructuredClaude:
    name = "claude_cli"

    def __init__(self, executable: str, model: str | None, timeout: float) -> None:
        self.executable = executable
        self.model = model
        self.timeout = timeout

    def call(self, system: str, user: str, schema: dict[str, Any]) -> StructuredReply:
        cmd = [
            self.executable,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            "--system-prompt",
            system,
            "--tools",
            "",  # pure reasoning: no file, shell or web tools
            "--strict-mcp-config",  # no MCP servers
            "--setting-sources",
            "",  # ignore user/project settings and hooks
            "--no-session-persistence",
        ]
        if self.model:
            cmd += ["--model", self.model]
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                cmd,
                input=user,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout,
                # Neutral working dir so no project CLAUDE.md is loaded.
                cwd=tempfile.gettempdir(),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCallError(f"Claude CLI timed out after {self.timeout:.0f}s") from exc
        except OSError as exc:
            raise ClaudeUnavailableError(f"Could not run Claude CLI: {exc}") from exc
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise ClaudeCallError(f"Claude CLI returned non-JSON output: {detail}") from exc
        if result.get("is_error") or result.get("subtype") != "success":
            raise ClaudeCallError(f"Claude CLI error: {str(result.get('result'))[:300]}")
        structured = result.get("structured_output")
        if structured is None:
            raise ClaudeCallError("Claude CLI returned no structured output")
        models = list((result.get("modelUsage") or {}).keys())
        return StructuredReply(payload=structured, model=models[0] if models else self.model)
