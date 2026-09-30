from pathlib import Path
from typing import Any

from agent.analysis.schema import JobAnalysisOutput
from backend.core.config import REPO_ROOT
from backend.core.profile import JobPreferences
from backend.services.shortlist import (
    ShortlistRules,
    evaluate,
    exclusion_check,
    load_shortlist_rules,
)


def output(**overrides: Any) -> JobAnalysisOutput:
    base = {
        "role_match": "strong",
        "skills_match": "medium",
        "experience_match": "medium",
        "location_match": "strong",
        "salary_match": "unknown",
        "freshness": "fresh",
        "matching_skills": [],
        "missing_skills": ["Go", "Kafka"],
        "important_requirements": [],
        "concerns": [],
        "match_score": 64,
        "should_shortlist": True,
        "reason": "ok",
    }
    return JobAnalysisOutput.model_validate({**base, **overrides})


def failed(results: list[dict[str, Any]]) -> list[str]:
    return [r["rule"] for r in results if not r["passed"]]


def test_shipped_rules_file_loads_with_expected_defaults() -> None:
    rules = load_shortlist_rules(REPO_ROOT / "config" / "shortlist_rules.yaml")
    assert rules.dimensions["role_match"].reject == ["weak"]
    assert rules.dimensions["salary_match"].reject == ["weak"]
    assert rules.freshness.reject == ["stale", "unknown"]  # only FRESH qualifies
    assert rules.min_match_score is None


def test_default_rules_accept_unknown_salary_and_missing_skills() -> None:
    ok, results = evaluate(output(), ShortlistRules())
    assert ok, results
    assert {r["rule"] for r in results} >= {
        "role_match",
        "experience_match",
        "location_match",
        "freshness",
    }


def test_weak_role_experience_or_location_rejects() -> None:
    for dim in ("role_match", "experience_match", "location_match"):
        ok, results = evaluate(output(**{dim: "weak"}), ShortlistRules())
        assert not ok and failed(results) == [dim]


def test_default_rules_accept_only_fresh() -> None:
    """The code default (used when the config file is missing) is the same policy."""
    assert evaluate(output(freshness="fresh"), ShortlistRules())[0]
    for level in ("stale", "unknown"):
        ok, results = evaluate(output(freshness=level), ShortlistRules())
        assert not ok and failed(results) == ["freshness"], level


def test_thresholds_are_configurable() -> None:
    rules = ShortlistRules(min_match_score=70, max_missing_skills=1)
    ok, results = evaluate(output(), rules)
    assert not ok and set(failed(results)) == {"min_match_score", "max_missing_skills"}


def test_analyzer_recommendation_can_be_required_or_ignored() -> None:
    assert not evaluate(output(should_shortlist=False), ShortlistRules())[0]
    assert evaluate(
        output(should_shortlist=False), ShortlistRules(require_analyzer_recommendation=False)
    )[0]


def test_rules_loaded_from_yaml(tmp_path: Path) -> None:
    f = tmp_path / "rules.yaml"
    f.write_text(
        "dimensions:\n  skills_match: {reject: [weak, unknown]}\nfreshness: {reject: []}\n"
    )
    rules = load_shortlist_rules(f)
    assert not evaluate(output(skills_match="unknown"), rules)[0]
    assert evaluate(output(freshness="stale"), rules)[0]
    assert load_shortlist_rules(tmp_path / "missing.yaml") == ShortlistRules()


def test_exclusions_from_preferences() -> None:
    prefs = JobPreferences(excluded_companies=["Legacy Systems Pvt Ltd"], excluded_roles=["Sales"])
    assert exclusion_check("Legacy Systems", "Software Engineer", prefs)
    assert exclusion_check("Acme", "Sales Engineer", prefs)
    assert exclusion_check("Acme", "Software Engineer", prefs) is None


def test_company_exclusion_ignores_case_punctuation_and_legal_suffixes() -> None:
    prefs = JobPreferences(excluded_companies=["Acme Labs.ai"])
    for name in [
        "Acme Labs.ai",
        "ACME LABS.AI",
        "Acme Labs AI",
        "Acme Labs-AI",
        "Acme Labs AI Pvt Ltd",
        "acme labs.ai Private Limited",
    ]:
        assert exclusion_check(name, "Software Engineer", prefs), name
    assert exclusion_check("MongoDB", "Software Engineer", prefs) is None
    assert exclusion_check("Etharium Labs", "Software Engineer", prefs) is None
