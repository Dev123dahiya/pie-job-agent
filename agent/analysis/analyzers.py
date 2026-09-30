"""Analyzer backends. Each returns raw model output; validation is shared (schema.validate_output).

- ClaudeApiAnalyzer: Anthropic API with structured outputs (needs ANTHROPIC_API_KEY).
- ClaudeCliAnalyzer: the local Claude Code CLI in print mode with a JSON schema; uses the
  user's own Claude subscription login. Runs with no tools, no MCP servers, no settings.
- HeuristicAnalyzer: deterministic keyword matching. NOT Claude; for tests/offline use and
  always labelled as such.
"""

import re
import shutil
from dataclasses import dataclass
from typing import Any, Protocol

from agent.analysis.prompt import SYSTEM_PROMPT, build_user_message, retry_feedback
from agent.analysis.schema import OUTPUT_JSON_SCHEMA, AnalysisJob
from agent.claude_client import (
    ApiStructuredClaude,
    ClaudeCallError,
    ClaudeUnavailableError,
    CliStructuredClaude,
    StructuredClaude,
)
from backend.core.config import Settings
from backend.core.normalize import title_matches
from backend.core.profile import UserProfile

# The analyzer's errors are the shared Claude-call errors.
AnalyzerError = ClaudeCallError
AnalyzerUnavailableError = ClaudeUnavailableError


@dataclass
class RawAnalysis:
    payload: str | dict[str, Any]
    model: str | None


class JobAnalyzer(Protocol):
    name: str  # claude_api | claude_cli | heuristic

    def analyze(
        self, job: AnalysisJob, profile: UserProfile, feedback: str | None = None
    ) -> RawAnalysis: ...


def _prompt(job: AnalysisJob, profile: UserProfile, feedback: str | None) -> str:
    return build_user_message(job, profile) + (retry_feedback(feedback) if feedback else "")


# --- Anthropic API ------------------------------------------------------------------------


class ClaudeApiAnalyzer:
    name = "claude_api"

    def __init__(self, api_key: str, model: str, effort: str, timeout: float) -> None:
        import anthropic

        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self.model = model
        self.effort = effort

    def analyze(
        self, job: AnalysisJob, profile: UserProfile, feedback: str | None = None
    ) -> RawAnalysis:
        reply = ApiStructuredClaude(self.client, self.model, self.effort).call(
            SYSTEM_PROMPT, _prompt(job, profile, feedback), OUTPUT_JSON_SCHEMA
        )
        return RawAnalysis(payload=reply.payload, model=reply.model)


# --- Claude Code CLI ----------------------------------------------------------------------


class ClaudeCliAnalyzer:
    name = "claude_cli"

    def __init__(self, executable: str, model: str | None, timeout: float) -> None:
        self.executable = executable
        self.model = model
        self.timeout = timeout

    def analyze(
        self, job: AnalysisJob, profile: UserProfile, feedback: str | None = None
    ) -> RawAnalysis:
        reply = CliStructuredClaude(self.executable, self.model, self.timeout).call(
            SYSTEM_PROMPT, _prompt(job, profile, feedback), OUTPUT_JSON_SCHEMA
        )
        return RawAnalysis(payload=reply.payload, model=reply.model)


# --- Heuristic (not Claude) ---------------------------------------------------------------


class HeuristicAnalyzer:
    """Deterministic keyword matcher. Conservative: says 'unknown' whenever data is missing."""

    name = "heuristic"
    model = "heuristic-v1"

    def __init__(
        self,
        location_patterns: dict[str, list[str]] | None = None,
        term_aliases: dict[str, list[str]] | None = None,
    ) -> None:
        self.location_patterns = location_patterns or {}
        self.term_aliases = term_aliases or {}

    def analyze(
        self, job: AnalysisJob, profile: UserProfile, feedback: str | None = None
    ) -> RawAnalysis:
        prefs, facts = profile.preferences, profile.verified_profile
        verified = {s.lower(): s for s in facts.all_skills()}

        role = (
            "strong"
            if title_matches(job.title, prefs.preferred_roles, self.term_aliases)
            else "weak"
        )
        if re.search(
            r"\b(senior|staff|principal|lead|director|head|manager)\b", job.title, re.I
        ) and (facts.total_experience_years < 3):
            role = "weak"

        job_skills = job.skills
        matching = [verified[s.lower()] for s in job_skills if s.lower() in verified]
        missing = [s for s in job_skills if s.lower() not in verified]
        if not job_skills:
            skills = "unknown"
        else:
            ratio = len(matching) / len(job_skills)
            skills = "strong" if ratio >= 0.6 else "medium" if ratio >= 0.3 else "weak"

        experience = "unknown"
        if job.experience_required:
            years = re.findall(r"\d+", job.experience_required)
            if years:
                need = int(years[0])
                have = facts.total_experience_years
                experience = "strong" if need <= have else "medium" if need <= have + 1 else "weak"
            elif re.search(r"fresher|entry|new grad", job.experience_required, re.I):
                experience = "strong"

        location = "unknown"
        if job.location:
            wanted = prefs.preferred_locations
            hit = any(
                re.search(p, job.location, re.I)
                for name in wanted
                for p in self.location_patterns.get(name, [re.escape(name)])
            )
            location = "strong" if hit else "weak"

        salary = "unknown"
        if job.has_salary and prefs.minimum_salary and job.salary_currency == prefs.salary_currency:
            top = job.salary_max or job.salary_min or 0
            salary = "strong" if top >= prefs.minimum_salary else "weak"

        scores = {"strong": 1.0, "medium": 0.6, "unknown": 0.5, "weak": 0.0}
        dims = [role, skills, experience, location, salary]
        score = round(100 * sum(scores[d] for d in dims) / len(dims))
        concerns = [
            f"{name} is weak"
            for name, d in zip(
                ["role", "skills", "experience", "location", "salary"], dims, strict=True
            )
            if d == "weak"
        ]
        should = "weak" not in (role, experience, location)
        return RawAnalysis(
            payload={
                "role_match": role,
                "skills_match": skills,
                "experience_match": experience,
                "location_match": location,
                "salary_match": salary,
                "freshness": job.freshness,
                "matching_skills": matching,
                "missing_skills": missing,
                "important_requirements": job.requirements[:8],
                "concerns": concerns,
                "match_score": score,
                "should_shortlist": should,
                "reason": "Heuristic keyword match (not a Claude analysis): "
                + ", ".join(
                    f"{n}={d}"
                    for n, d in zip(
                        ["role", "skills", "experience", "location", "salary"], dims, strict=True
                    )
                ),
            },
            model=self.model,
        )


# --- Resolution ---------------------------------------------------------------------------


def resolve_analyzer(
    settings: Settings,
    location_patterns: dict[str, list[str]] | None = None,
    term_aliases: dict[str, list[str]] | None = None,
) -> JobAnalyzer:
    backend = settings.analyzer_backend.lower()
    if backend == "heuristic":
        return HeuristicAnalyzer(location_patterns, term_aliases)
    if backend in ("auto", "claude_api") and settings.anthropic_api_key:
        return ClaudeApiAnalyzer(
            settings.anthropic_api_key,
            settings.claude_model,
            settings.claude_effort,
            settings.claude_timeout_seconds,
        )
    if backend == "claude_api":
        raise AnalyzerUnavailableError(
            "ANALYZER_BACKEND=claude_api but ANTHROPIC_API_KEY is not set"
        )
    if backend in ("auto", "claude_cli"):
        exe = shutil.which(settings.claude_cli_path)
        if exe:
            return ClaudeCliAnalyzer(
                exe, settings.claude_cli_model, settings.claude_timeout_seconds
            )
        raise AnalyzerUnavailableError(
            "No Claude analyzer available: set ANTHROPIC_API_KEY, or install/sign in to the "
            "Claude Code CLI (`claude`), or set ANALYZER_BACKEND=heuristic for offline testing."
        )
    raise AnalyzerUnavailableError(f"Unknown ANALYZER_BACKEND {settings.analyzer_backend!r}")


def resolve_structured_claude(settings: Settings) -> StructuredClaude:
    """The Claude backend for the agent. The heuristic has no agent equivalent."""
    backend = settings.analyzer_backend.lower()
    if backend == "heuristic":
        # Offline/test mode never falls back to a real Claude call.
        raise AnalyzerUnavailableError("The agent needs Claude; ANALYZER_BACKEND is 'heuristic'")
    if backend in ("auto", "claude_api") and settings.anthropic_api_key:
        return ApiStructuredClaude.from_key(
            settings.anthropic_api_key,
            settings.claude_model,
            settings.claude_effort,
            settings.claude_timeout_seconds,
        )
    if backend == "claude_api":
        raise AnalyzerUnavailableError(
            "ANALYZER_BACKEND=claude_api but ANTHROPIC_API_KEY is not set"
        )
    exe = shutil.which(settings.claude_cli_path)
    if exe:
        return CliStructuredClaude(exe, settings.claude_cli_model, settings.claude_timeout_seconds)
    raise AnalyzerUnavailableError(
        "No Claude backend available for the agent: set ANTHROPIC_API_KEY, or install and "
        "sign in to the Claude Code CLI (`claude`)."
    )


def describe_analyzer(settings: Settings) -> tuple[str, str]:
    """(status, detail) for the health endpoint, without making any model calls."""
    backend = settings.analyzer_backend.lower()
    if backend == "heuristic":
        return "not_configured", "Heuristic analyzer (not Claude)"
    if backend in ("auto", "claude_api") and settings.anthropic_api_key:
        return "ok", f"Anthropic API ({settings.claude_model})"
    if backend in ("auto", "claude_cli") and shutil.which(settings.claude_cli_path):
        return "ok", "Claude Code CLI (your Claude subscription)"
    return "not_configured", "No Claude backend available"
