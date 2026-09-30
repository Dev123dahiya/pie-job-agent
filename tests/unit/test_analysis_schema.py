"""Structured-output validation: malformed or ungrounded model output is rejected."""

import json
from typing import Any

import pytest

from agent.analysis.schema import AnalysisJob, AnalysisValidationError, validate_output
from backend.services.freshness import compute_freshness
from database.enums import Freshness

SKILLS = ["Python", "SQL", "Node.js"]


def job(**overrides: Any) -> AnalysisJob:
    base: dict[str, Any] = dict(
        title="Software Engineer",
        company="Acme",
        location="Gurugram",
        remote_type="unknown",
        salary_min=None,
        salary_max=None,
        salary_currency=None,
        experience_required="0-2 years",
        description="Build things in Python.",
        requirements=[],
        skills=["Python", "Go"],
        posted_at=None,
        freshness="unknown",
    )
    return AnalysisJob(**{**base, **overrides})


def good(**overrides: Any) -> dict[str, Any]:
    base = {
        "role_match": "strong",
        "skills_match": "medium",
        "experience_match": "strong",
        "location_match": "strong",
        "salary_match": "unknown",
        "freshness": "unknown",
        "matching_skills": ["Python"],
        "missing_skills": ["Go"],
        "important_requirements": ["0-2 years"],
        "concerns": [],
        "match_score": 78,
        "should_shortlist": True,
        "reason": "Entry-level Python role in Gurugram.",
    }
    return {**base, **overrides}


def test_valid_output_accepted_from_dict_and_json_string() -> None:
    assert validate_output(good(), job(), SKILLS).match_score == 78
    assert validate_output(json.dumps(good()), job(), SKILLS).skills_match == "medium"


def test_skill_spelling_variants_are_accepted() -> None:
    out = validate_output(good(matching_skills=["python", "NodeJS"]), job(), SKILLS)
    assert out.matching_skills == ["python", "NodeJS"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("not json at all", "not valid JSON"),
        ("[1, 2]", "JSON object"),
        (good(extra_field=1), "extra_field"),
        (good(role_match="excellent"), "role_match"),
        (good(match_score=150), "match_score"),
        (good(match_score="80"), "match_score"),  # strict: no string coercion
        (good(should_shortlist="yes"), "should_shortlist"),
        (good(reason=""), "reason"),
        ({k: v for k, v in good().items() if k != "concerns"}, "concerns"),
    ],
)
def test_malformed_output_rejected(payload: Any, message: str) -> None:
    with pytest.raises(AnalysisValidationError, match=message):
        validate_output(payload, job(), SKILLS)


def test_invented_candidate_skill_rejected() -> None:
    with pytest.raises(AnalysisValidationError, match="not in profile: \\['Kubernetes'\\]"):
        validate_output(good(matching_skills=["Python", "Kubernetes"]), job(), SKILLS)


def test_salary_must_be_unknown_when_job_has_no_salary() -> None:
    with pytest.raises(AnalysisValidationError, match="salary_match must be 'unknown'"):
        validate_output(good(salary_match="strong"), job(), SKILLS)
    paid = job(salary_min=1_200_000, salary_max=1_800_000, salary_currency="INR")
    assert validate_output(good(salary_match="strong"), paid, SKILLS).salary_match == "strong"


def test_experience_must_be_unknown_without_any_information() -> None:
    bare = job(experience_required=None, description=None)
    with pytest.raises(AnalysisValidationError, match="experience_match"):
        validate_output(good(experience_match="strong"), bare, SKILLS)
    assert validate_output(good(experience_match="unknown"), bare, SKILLS)


def test_freshness_must_match_the_computed_value() -> None:
    with pytest.raises(AnalysisValidationError, match="freshness must be 'stale'"):
        validate_output(good(freshness="fresh"), job(freshness="stale"), SKILLS)


def test_compute_freshness() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 9, 28, 12, tzinfo=UTC)
    assert compute_freshness(None, now, 48) is Freshness.UNKNOWN
    assert compute_freshness(now - timedelta(hours=47, minutes=59), now, 48) is Freshness.FRESH
    assert compute_freshness(now - timedelta(hours=48, minutes=1), now, 48) is Freshness.STALE
    assert compute_freshness(now - timedelta(hours=30), now, 24) is Freshness.STALE
