"""Configurable shortlist rules (config/shortlist_rules.yaml). No hardcoded magic numbers."""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from agent.analysis.schema import JobAnalysisOutput
from backend.core.normalize import normalize_company, normalize_title
from backend.core.profile import JobPreferences

Dimension = Literal[
    "role_match", "skills_match", "experience_match", "location_match", "salary_match"
]
Level = Literal["strong", "medium", "weak", "unknown"]


class DimensionRule(BaseModel):
    reject: list[Level] = Field(default_factory=list)


FreshnessLevel = Literal["fresh", "stale", "unknown"]


def _reject_not_fresh() -> list[FreshnessLevel]:
    # Only FRESH qualifies: a job needs trustworthy evidence it was posted within the window.
    # This default applies when config/shortlist_rules.yaml is missing or has no freshness
    # section, so neither can silently make UNKNOWN (or STALE) eligible.
    return ["stale", "unknown"]


def _default_dimensions() -> dict[Dimension, DimensionRule]:
    return {
        "role_match": DimensionRule(reject=["weak"]),
        "experience_match": DimensionRule(reject=["weak"]),
        "location_match": DimensionRule(reject=["weak"]),
    }


class FreshnessRule(BaseModel):
    reject: list[FreshnessLevel] = Field(default_factory=_reject_not_fresh)


class ShortlistRules(BaseModel):
    dimensions: dict[Dimension, DimensionRule] = Field(default_factory=_default_dimensions)
    freshness: FreshnessRule = Field(default_factory=FreshnessRule)
    min_match_score: int | None = Field(default=None, ge=0, le=100)
    max_missing_skills: int | None = Field(default=None, ge=0)
    require_analyzer_recommendation: bool = True
    analyze_stale_jobs_in_batch: bool = False


def load_shortlist_rules(path: Path) -> ShortlistRules:
    if not path.exists():
        return ShortlistRules()
    with path.open(encoding="utf-8") as fh:
        return ShortlistRules.model_validate(yaml.safe_load(fh) or {})


# The final match score (user's choice 2026-10-04) is the average of Claude's overall score and
# a fixed-weight score from the five dimensions, so every score can be explained.
DIMENSION_WEIGHTS = {
    "role_match": 30,
    "experience_match": 25,
    "skills_match": 25,
    "location_match": 15,
    "salary_match": 5,
}
LEVEL_VALUE = {"strong": 1.0, "medium": 0.6, "unknown": 0.5, "weak": 0.0}


def dimension_score(levels: dict[str, str]) -> int:
    """0-100 from the five dimensions with fixed weights (strong 1, medium 0.6, unknown 0.5,
    weak 0)."""
    total = sum(w * LEVEL_VALUE.get(str(levels.get(d)), 0.5) for d, w in DIMENSION_WEIGHTS.items())
    return round(100 * total / sum(DIMENSION_WEIGHTS.values()))


def combined_score(claude_score: int, levels: dict[str, str]) -> tuple[int, int]:
    """(final score, dimension score): the average of Claude's score and the dimension score."""
    dims = dimension_score(levels)
    return round((claude_score + dims) / 2), dims


def _levels(output: Any) -> dict[str, str]:
    return {d: str(getattr(output, d)) for d in DIMENSION_WEIGHTS}


def _result(rule: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"rule": rule, "passed": passed, "detail": detail}


def exclusion_check(company: str | None, title: str, prefs: JobPreferences) -> str | None:
    """Reason the job is excluded by the user's preferences, or None. Checked before analysis."""
    if company:
        norm = normalize_company(company)
        for excluded in prefs.excluded_companies:
            if normalize_company(excluded) == norm:
                return f"Company '{company}' is in excluded_companies"
    norm_title = f" {normalize_title(title)} "
    for excluded in prefs.excluded_roles:
        if f" {normalize_title(excluded)} " in norm_title:
            return f"Title matches excluded role '{excluded}'"
    return None


def evaluate(output: JobAnalysisOutput, rules: ShortlistRules) -> tuple[bool, list[dict[str, Any]]]:
    """Apply every rule; the job is shortlisted only if all pass. Returns (decision, results)."""
    results: list[dict[str, Any]] = []
    for dimension, rule in rules.dimensions.items():
        level = getattr(output, dimension)
        passed = level not in rule.reject
        results.append(
            _result(
                dimension, passed, f"{level}" + ("" if passed else f" (rejected: {rule.reject})")
            )
        )

    passed = output.freshness not in rules.freshness.reject
    results.append(
        _result("freshness", passed, output.freshness + ("" if passed else " (rejected)"))
    )

    final, dims = combined_score(output.match_score, _levels(output))
    if rules.min_match_score is not None:
        ok = final >= rules.min_match_score
        results.append(
            {
                **_result(
                    "min_match_score",
                    ok,
                    f"{final} (Claude {output.match_score}, dimensions {dims}) "
                    f"vs minimum {rules.min_match_score}",
                ),
                "claude_score": output.match_score,
                "dimension_score": dims,
                "final_score": final,
            }
        )
    if rules.max_missing_skills is not None:
        n = len(output.missing_skills)
        ok = n <= rules.max_missing_skills
        results.append(
            _result("max_missing_skills", ok, f"{n} missing vs maximum {rules.max_missing_skills}")
        )
    if rules.require_analyzer_recommendation:
        results.append(
            _result(
                "analyzer_recommendation",
                output.should_shortlist,
                "analyzer recommends shortlisting"
                if output.should_shortlist
                else "analyzer advises against",
            )
        )
    return all(r["passed"] for r in results), results
