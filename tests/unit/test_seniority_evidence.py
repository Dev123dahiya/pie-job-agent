"""Seniority evidence from a posting's own title/text (queue ordering only - never eligibility).

The candidate's verified full-time experience is 0.4 years. Senior evidence always wins over
entry-level wording; internships never count as entry-level full-time evidence."""

import pytest

from backend.core.normalize import required_years, seniority_evidence

YEARS = 0.4


@pytest.mark.parametrize(
    "title",
    ["Graduate Software Engineer", "SDE I", "SDE-1", "Software Engineer 1", "Software Engineer I",
     "Junior Backend Developer", "Engineer Trainee", "Entry Level Data Engineer"],
)  # fmt: skip
def test_entry_level_titles(title: str) -> None:
    assert seniority_evidence(title, "Build APIs.", YEARS) == "entry"


@pytest.mark.parametrize(
    "text",
    ["Freshers are welcome to apply.", "0-1 years of experience", "0 to 2 yrs experience",
     "Open to new grads and recent graduates.", "No prior experience required."],
)  # fmt: skip
def test_entry_level_wording(text: str) -> None:
    assert seniority_evidence("Software Engineer", text, YEARS) == "entry"


@pytest.mark.parametrize(
    "title",
    ["Senior Software Engineer", "Staff Engineer", "Lead Data Engineer", "Software Engineer II",
     "SDE 2", "Software Engineer 3", "Engineering Manager", "Principal ML Engineer",
     "Senior Engineer I", "Staff Engineer I - Platform"],
)  # fmt: skip
def test_senior_titles_win_even_over_entry_words(title: str) -> None:
    assert seniority_evidence(title, "Freshers welcome.", YEARS) == "senior"


def test_explicit_years_above_the_candidate_are_senior_whatever_the_title() -> None:
    assert (
        seniority_evidence("Junior Developer", "2+ years of experience in Java", YEARS) == "senior"
    )
    assert seniority_evidence("Graduate Engineer", "Minimum 1 year experience", YEARS) == "senior"
    assert (
        seniority_evidence("Software Engineer", "3-5 years relevant experience", YEARS) == "senior"
    )


def test_years_at_or_below_the_candidate_are_not_senior() -> None:
    assert seniority_evidence("Software Engineer", "0-1 years of experience", YEARS) == "entry"
    assert seniority_evidence("Software Engineer", "0+ years experience", YEARS) == "none"


def test_numbers_that_are_not_experience_requirements_are_ignored() -> None:
    text = "Founded 10 years ago, 5 years of profitable growth. Work with senior engineers."
    assert required_years(text) is None
    assert seniority_evidence("Software Engineer", text, YEARS) == "none"


def test_internships_are_never_entry_level_full_time_evidence() -> None:
    assert seniority_evidence("Software Engineering Intern", "Freshers welcome.", YEARS) == "none"
    assert seniority_evidence("Data Science Internship", None, YEARS) == "none"


def test_associate_titles_are_not_trusted_as_entry_level() -> None:
    assert seniority_evidence("Associate Software Engineer", "Build APIs.", YEARS) == "none"
    assert (
        seniority_evidence("Associate Software Engineer", "2+ years experience", YEARS) == "senior"
    )


def test_without_candidate_years_explicit_years_are_not_judged() -> None:
    assert seniority_evidence("Software Engineer", "2+ years of experience", None) == "none"


def test_required_years_takes_the_smallest_explicit_requirement() -> None:
    assert required_years("5+ years of experience; ideally 7 years experience") == 5.0
    assert required_years("4 to 8 years of experience") == 4.0
    assert required_years(None) is None
