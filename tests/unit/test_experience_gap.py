"""Experience gap: required vs verified full-time experience, reported and never concealed."""

from typing import Any

import pytest

from agent.tailoring.experience import (
    experience_claims,
    experience_gap,
    parse_required_years,
)
from agent.tailoring.schema import parse_tailored
from agent.tailoring.validation import Evidence, validate_tailored
from backend.core.profile import UserProfile
from tests.conftest import MASTER_RESUME_MD, good_resume


def job(**over: Any) -> dict[str, Any]:
    return {"experience_required": None, "requirements": [], **over}


def with_years(profile: UserProfile, years: float) -> UserProfile:
    facts = profile.verified_profile.model_copy(update={"total_experience_years": years})
    return profile.model_copy(update={"verified_profile": facts})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2-5 years", (2.0, 5.0)),
        ("8 to 10 yrs", (8.0, 10.0)),
        ("3+ years", (3.0, None)),
        ("3 or more years", (3.0, None)),
        ("two years of experience", (2.0, None)),
        ("6 months", (0.5, None)),
        ("Fresher / entry-level", (0.0, None)),
        ("Strong technical judgment", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_required_years(text: str | None, expected: Any) -> None:
    assert parse_required_years(text) == expected


def test_candidate_has_less_experience_than_required(tailor_profile: UserProfile) -> None:
    gap = experience_gap(tailor_profile, job(experience_required="2-5 years"))
    assert gap["gap"] is True and gap["status"] == "gap"
    assert gap["required"] == "2-5 years"  # the job's own wording, unchanged
    assert gap["candidate"] == "0.4 years" and gap["candidate_years"] == 0.4
    assert gap["shortfall_years"] == 1.6
    assert "never concealed" in gap["note"] and "shortlist eligibility" in gap["note"]


def test_candidate_meets_requirement(tailor_profile: UserProfile) -> None:
    for required in ("0-2 years", "Fresher", "6 months"):
        gap = experience_gap(with_years(tailor_profile, 0.5), job(experience_required=required))
        assert gap["gap"] is False and gap["status"] == "meets", required


def test_candidate_exceeds_requirement(tailor_profile: UserProfile) -> None:
    gap = experience_gap(with_years(tailor_profile, 4), job(experience_required="0-2 years"))
    assert gap["gap"] is False and gap["status"] == "exceeds"
    # An open-ended "N+" requirement is met, not exceeded.
    open_ended = experience_gap(with_years(tailor_profile, 4), job(experience_required="2+ years"))
    assert open_ended["status"] == "meets"


def test_unclear_or_absent_requirement_is_unknown(tailor_profile: UserProfile) -> None:
    for posting in (
        job(),
        job(experience_required="Relevant industry experience"),
        job(requirements=["Strong technical judgment", "Comfortable with ambiguity"]),
    ):
        gap = experience_gap(tailor_profile, posting)
        assert gap["gap"] == "unknown" and gap["status"] == "unknown"
        assert gap["required_min_years"] is None and gap["shortfall_years"] is None


def test_requirement_found_in_requirements_list(tailor_profile: UserProfile) -> None:
    posting = job(requirements=["Python", "2-5 years of professional software experience"])
    gap = experience_gap(tailor_profile, posting)
    assert gap["gap"] is True and gap["required_source"] == "job.requirements"


def test_internships_are_listed_but_never_counted_as_full_time(
    tailor_profile: UserProfile,
) -> None:
    gap = experience_gap(tailor_profile, job(experience_required="1+ years"))
    # The fixture has a full-time role and an internship; only verified full-time years count.
    assert gap["internships"] and all("Intern" in i for i in gap["internships"])
    assert not any("Software Developer at" in i for i in gap["internships"])
    assert gap["internships_counted_as_full_time"] is False
    assert gap["candidate_years"] == tailor_profile.verified_profile.total_experience_years
    assert gap["gap"] is True  # 0.4 < 1 even though an internship exists


def test_experience_claims_are_extracted() -> None:
    claims = experience_claims("Engineer with 2+ years of professional experience; 24+ FPS")
    assert claims == [("2+ years of professional experience", 2.0)]
    assert experience_claims("Built a pipeline in 6 months") == []


def evidence(profile: UserProfile) -> Evidence:
    return Evidence(master_text=MASTER_RESUME_MD, profile=profile)


def test_fabricated_years_of_experience_are_rejected(tailor_profile: UserProfile) -> None:
    for summary in (
        "Software developer with 2 years of experience building REST APIs with FastAPI.",
        "Software developer with two years of professional experience in Python.",
    ):
        violations = validate_tailored(
            parse_tailored(good_resume(summary=summary)), evidence(tailor_profile)
        )
        assert "UNSUPPORTED_EXPERIENCE" in {v.code for v in violations}, summary


def test_verified_experience_claim_is_allowed(tailor_profile: UserProfile) -> None:
    resume = good_resume(summary="Software developer with 0.4 years of full-time experience.")
    violations = validate_tailored(parse_tailored(resume), evidence(tailor_profile))
    assert "UNSUPPORTED_EXPERIENCE" not in {v.code for v in violations}


def test_internship_cannot_be_retitled_as_full_time(tailor_profile: UserProfile) -> None:
    resume = good_resume()
    resume["experience"][1]["title"] = "Backend Engineering"  # "Intern" dropped
    violations = validate_tailored(parse_tailored(resume), evidence(tailor_profile))
    assert "UNSUPPORTED_TITLE" in {v.code for v in violations}
