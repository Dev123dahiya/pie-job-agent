"""Analyzer backends with the network/subprocess faked. No real Claude calls are made."""

import json
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from agent.analysis import analyzers
from agent.analysis.analyzers import (
    AnalyzerError,
    AnalyzerUnavailableError,
    ClaudeApiAnalyzer,
    ClaudeCliAnalyzer,
    HeuristicAnalyzer,
    resolve_analyzer,
)
from agent.analysis.prompt import SYSTEM_PROMPT, build_user_message
from agent.analysis.schema import AnalysisJob, validate_output
from backend.core.config import Settings
from backend.core.profile import UserProfile

JOB = AnalysisJob(
    title="Backend Software Engineer",
    company="Nimbus",
    location="Gurugram, India",
    remote_type="unknown",
    salary_min=1_200_000,
    salary_max=1_800_000,
    salary_currency="INR",
    experience_required="0-2 years",
    description="Python, SQL, FastAPI",
    requirements=["Python"],
    skills=["Python", "SQL", "FastAPI", "Kafka"],
    posted_at=None,
    freshness="unknown",
)
OUTPUT = {
    "role_match": "strong",
    "skills_match": "strong",
    "experience_match": "strong",
    "location_match": "strong",
    "salary_match": "strong",
    "freshness": "unknown",
    "matching_skills": ["Python", "SQL"],
    "missing_skills": ["Kafka"],
    "important_requirements": ["Python"],
    "concerns": [],
    "match_score": 85,
    "should_shortlist": True,
    "reason": "Fits.",
}


def test_prompt_separates_facts_from_preferences(profile: UserProfile) -> None:
    message = build_user_message(JOB, profile)
    assert (
        message.index("VERIFIED_PROFILE:") < message.index("PREFERENCES:") < message.index("JOB:")
    )
    verified = json.loads(message.split("VERIFIED_PROFILE:\n")[1].split("\n\nPREFERENCES:")[0])
    assert "preferred_roles" not in verified  # preferences never leak into the facts section
    assert "email" not in verified and "phone" not in verified  # contact details not needed
    assert "Never invent" in SYSTEM_PROMPT


def _cli_result(**overrides: Any) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": OUTPUT,
            "modelUsage": {"claude-opus-5": {}},
            **overrides,
        }
    )


def test_cli_analyzer_success(monkeypatch: pytest.MonkeyPatch, profile: UserProfile) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        captured["cmd"], captured["input"] = cmd, kwargs["input"]
        return SimpleNamespace(stdout=_cli_result(), stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    raw = ClaudeCliAnalyzer("claude", None, 30).analyze(JOB, profile)
    assert raw.model == "claude-opus-5"
    assert (
        validate_output(raw.payload, JOB, profile.verified_profile.all_skills()).match_score == 85
    )
    cmd = captured["cmd"]
    assert cmd[cmd.index("--tools") + 1] == ""  # no tools: pure reasoning
    assert "--json-schema" in cmd and "--strict-mcp-config" in cmd
    assert "VERIFIED_PROFILE" in captured["input"]


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        ("Error: not logged in", "non-JSON"),
        (_cli_result(is_error=True, subtype="error", result="auth failed"), "auth failed"),
        (_cli_result(structured_output=None), "no structured output"),
    ],
)
def test_cli_analyzer_errors(
    monkeypatch: pytest.MonkeyPatch, profile: UserProfile, stdout: str, message: str
) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=stdout, stderr="")
    )
    with pytest.raises(AnalyzerError, match=message):
        ClaudeCliAnalyzer("claude", None, 30).analyze(JOB, profile)


def test_cli_analyzer_timeout(monkeypatch: pytest.MonkeyPatch, profile: UserProfile) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=30)

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(AnalyzerError, match="timed out"):
        ClaudeCliAnalyzer("claude", None, 30).analyze(JOB, profile)


class _FakeMessages:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.response


def _api(response: Any) -> tuple[ClaudeApiAnalyzer, _FakeMessages]:
    analyzer = ClaudeApiAnalyzer("sk-test", "claude-opus-5", "medium", 30)
    fake = _FakeMessages(response)
    analyzer.client = SimpleNamespace(beta=SimpleNamespace(messages=fake))  # type: ignore[assignment]
    return analyzer, fake


def test_api_analyzer_requests_structured_output(profile: UserProfile) -> None:
    response = SimpleNamespace(
        stop_reason="end_turn",
        model="claude-opus-5",
        content=[SimpleNamespace(type="text", text=json.dumps(OUTPUT))],
    )
    analyzer, fake = _api(response)
    raw = analyzer.analyze(JOB, profile)
    assert json.loads(raw.payload)["match_score"] == 85  # type: ignore[arg-type]
    fmt = fake.kwargs["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False
    assert fake.kwargs["model"] == "claude-opus-5"


@pytest.mark.parametrize(("stop", "message"), [("refusal", "declined"), ("max_tokens", "cut off")])
def test_api_analyzer_stop_reasons(profile: UserProfile, stop: str, message: str) -> None:
    analyzer, _ = _api(SimpleNamespace(stop_reason=stop, model="m", content=[]))
    with pytest.raises(AnalyzerError, match=message):
        analyzer.analyze(JOB, profile)


def test_heuristic_is_labelled_and_grounded(profile: UserProfile) -> None:
    raw = HeuristicAnalyzer().analyze(JOB, profile)
    out = validate_output(raw.payload, JOB, profile.verified_profile.all_skills())
    assert raw.model == "heuristic-v1"
    assert out.reason.startswith("Heuristic keyword match (not a Claude analysis)")
    assert out.matching_skills == ["Python", "SQL", "FastAPI"] and out.missing_skills == ["Kafka"]


def test_resolve_analyzer(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_analyzer(Settings(analyzer_backend="heuristic")).name == "heuristic"
    assert (
        resolve_analyzer(Settings(analyzer_backend="auto", anthropic_api_key="sk-x")).name
        == "claude_api"
    )
    with pytest.raises(AnalyzerUnavailableError, match="ANTHROPIC_API_KEY"):
        resolve_analyzer(Settings(analyzer_backend="claude_api", anthropic_api_key=None))
    monkeypatch.setattr(analyzers.shutil, "which", lambda name: "/usr/bin/claude")
    assert (
        resolve_analyzer(Settings(analyzer_backend="auto", anthropic_api_key=None)).name
        == "claude_cli"
    )
    monkeypatch.setattr(analyzers.shutil, "which", lambda name: None)
    with pytest.raises(AnalyzerUnavailableError, match="No Claude analyzer available"):
        resolve_analyzer(Settings(analyzer_backend="auto", anthropic_api_key=None))


def test_prompt_omits_unconfirmed_skills(profile: UserProfile) -> None:
    profile.verified_profile.skills.append("Go")
    profile.verified_profile.unconfirmed_skills = ["Go"]
    message = build_user_message(JOB, profile)
    verified = json.loads(message.split("VERIFIED_PROFILE:\n")[1].split("\n\nPREFERENCES:")[0])
    assert "Go" not in verified["skills"]
    assert "unconfirmed_skills" not in verified
