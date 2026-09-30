"""Input/output contracts for job analysis, and the validation that guards them.

The model's output is rejected - never silently patched - when it is malformed or when it
contradicts the facts it was given (e.g. claims a skill the verified profile does not list).
"""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Level = Literal["strong", "medium", "weak", "unknown"]
FreshnessLevel = Literal["fresh", "stale", "unknown"]


class AnalysisJob(BaseModel):
    """The structured job facts the analyzer sees (never raw HTML)."""

    title: str
    company: str | None
    location: str | None
    remote_type: str
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    experience_required: str | None
    description: str | None
    requirements: list[str]
    skills: list[str]
    posted_at: str | None
    freshness: FreshnessLevel  # computed by PIE, not by the model

    @property
    def has_salary(self) -> bool:
        return self.salary_min is not None or self.salary_max is not None


class JobAnalysisOutput(BaseModel):
    """Exact shape the analyzer must return. Extra keys are an error."""

    model_config = ConfigDict(extra="forbid", strict=True)

    role_match: Level
    skills_match: Level
    experience_match: Level
    location_match: Level
    salary_match: Level
    freshness: FreshnessLevel
    matching_skills: list[str]
    missing_skills: list[str]
    important_requirements: list[str]
    concerns: list[str]
    match_score: int = Field(ge=0, le=100)
    should_shortlist: bool
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("matching_skills", "missing_skills", "important_requirements", "concerns")
    @classmethod
    def _clean_list(cls, value: list[str]) -> list[str]:
        if len(value) > 40:
            raise ValueError("too many items")
        return [v.strip() for v in value if v.strip()]


_LEVEL = {"type": "string", "enum": ["strong", "medium", "weak", "unknown"]}
_STR_LIST = {"type": "array", "items": {"type": "string"}}

# Hand-written JSON Schema for structured outputs (numeric bounds are enforced by Pydantic).
OUTPUT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "role_match": _LEVEL,
        "skills_match": _LEVEL,
        "experience_match": _LEVEL,
        "location_match": _LEVEL,
        "salary_match": _LEVEL,
        "freshness": {"type": "string", "enum": ["fresh", "stale", "unknown"]},
        "matching_skills": _STR_LIST,
        "missing_skills": _STR_LIST,
        "important_requirements": _STR_LIST,
        "concerns": _STR_LIST,
        "match_score": {"type": "integer"},
        "should_shortlist": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": [
        "role_match", "skills_match", "experience_match", "location_match", "salary_match",
        "freshness", "matching_skills", "missing_skills", "important_requirements", "concerns",
        "match_score", "should_shortlist", "reason",
    ],
    "additionalProperties": False,
}  # fmt: skip


class AnalysisValidationError(ValueError):
    """The analyzer's output was malformed or not grounded in the provided facts."""


def _norm(skill: str) -> str:
    return " ".join(skill.lower().replace(".js", "js").split())


def validate_output(
    payload: str | dict[str, Any], job: AnalysisJob, verified_skills: list[str]
) -> JobAnalysisOutput:
    """Parse + validate. Raises AnalysisValidationError with every problem found."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AnalysisValidationError(f"Output is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AnalysisValidationError("Output must be a JSON object")
    try:
        output = JobAnalysisOutput.model_validate(payload)
    except ValidationError as exc:
        detail = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise AnalysisValidationError(f"Output failed schema validation: {detail}") from exc

    problems: list[str] = []
    if output.freshness != job.freshness:
        problems.append(
            f"freshness must be {job.freshness!r} as provided, got {output.freshness!r}"
        )
    if not job.has_salary and output.salary_match != "unknown":
        problems.append("salary_match must be 'unknown' because the job states no salary")
    if not job.experience_required and not job.description and output.experience_match != "unknown":
        problems.append(
            "experience_match must be 'unknown' because the job gives no experience info"
        )
    known = {_norm(s) for s in verified_skills}
    invented = [s for s in output.matching_skills if _norm(s) not in known]
    if invented:
        problems.append(
            "matching_skills may only contain skills from the verified profile; "
            f"not in profile: {invented}"
        )
    if problems:
        raise AnalysisValidationError(
            "Output not grounded in the provided facts: " + "; ".join(problems)
        )
    return output
