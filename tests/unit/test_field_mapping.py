"""Answer bank statuses and deterministic field classification (no guessing)."""

from typing import Any

import pytest

from agent.execution.answers import AnswerBank, question_key
from agent.execution.mapping import (
    AMBIGUOUS,
    CHALLENGE,
    SENSITIVE,
    UNKNOWN,
    VERIFIED_AUTOFILL,
    choose_option,
    classify,
    plan_field,
)
from backend.core.profile import UserProfile
from database.enums import AnswerStatus
from database.models import ApplicationAnswer


def profile_with(base: UserProfile, **update: Any) -> UserProfile:
    facts = base.verified_profile.model_copy(update=update)
    return base.model_copy(update={"verified_profile": facts})


@pytest.fixture
def real_like(tailor_profile: UserProfile) -> UserProfile:
    """A realistic (fictional) answer set: 4 verified answers, a three-word name, links."""
    return profile_with(
        tailor_profile,
        name="Asha Rani Verma",
        links={"linkedin": "https://linkedin.example/in/test"},
        verified_answers={
            "work_authorization": "Yes - authorized to work in India",
            "willing_to_relocate": "Yes",
            "notice_period": "Immediate",
            "expected_salary": "10 LPA",
        },
    )


@pytest.fixture
def bank(real_like: UserProfile) -> AnswerBank:
    return AnswerBank(real_like)


def field(label: str = "", kind: str = "text", **over: Any) -> dict[str, Any]:
    return {
        "id": "f0_1",
        "kind": kind,
        "label": label,
        "name": "",
        "html_id": "",
        "placeholder": "",
        "autocomplete": "",
        "required": True,
        "options": [],
        "surrounding": "",
        **over,
    }


def yes_no(label: str, kind: str = "select") -> dict[str, Any]:
    opts = [{"label": "Select..."}, {"label": "Yes"}, {"label": "No"}]
    return field(label, kind, options=opts)


# --- answers ------------------------------------------------------------------------------


def test_only_the_confirmed_answers_are_verified(bank: AnswerBank) -> None:
    assert bank.get("work_authorization").value == "Yes - authorized to work in India"
    assert bank.get("willing_to_relocate").value == "Yes"
    assert bank.get("notice_period").value == "Immediate"
    assert bank.get("expected_salary").value == "10 LPA"
    for invented in ("sponsorship", "citizenship", "gender", "disability", "veteran_status"):
        assert bank.get(invented).status is AnswerStatus.UNKNOWN, invented


def test_three_word_name_is_not_split(bank: AnswerBank) -> None:
    first = bank.get("first_name")
    assert first.status is AnswerStatus.UNCONFIRMED and "3 words" in (first.note or "")
    assert bank.get("full_name").verified and bank.get("full_name").value == "Asha Rani Verma"


def test_names_come_from_the_verified_identity(real_like: UserProfile) -> None:
    two = AnswerBank(profile_with(real_like, name="Test Candidate"))
    assert two.get("first_name").value == "Test" and two.get("last_name").value == "Candidate"
    explicit = AnswerBank(profile_with(real_like, first_name="Asha Rani", last_name="Verma"))
    assert explicit.get("first_name").value == "Asha Rani" and explicit.get("first_name").verified


def test_contradictory_name_sources_are_ambiguous(real_like: UserProfile) -> None:
    p = profile_with(real_like, name="Test Candidate", verified_answers={"first_name": "Other"})
    bank = AnswerBank(p)
    assert bank.get("first_name").conflict and not bank.get("first_name").verified
    plan = plan_field(field("First Name"), bank, None, True)
    assert plan.category == AMBIGUOUS and plan.action == "block"


def test_user_answers_are_verified_and_scoped(real_like: UserProfile) -> None:
    key = question_key("Are you willing to work night shifts?")
    rows = [
        ApplicationAnswer(
            application_id=None,
            question_key=key,
            question="q",
            answer="No",
            status=AnswerStatus.VERIFIED,
            source="answered by the user",
        ),
        ApplicationAnswer(
            application_id="other",
            question_key="q_x",
            question="q",
            answer="secret",
            status=AnswerStatus.VERIFIED,
            source="answered by the user",
        ),
    ]
    bank = AnswerBank(real_like, rows, application_id="mine")
    assert bank.get(key).verified and bank.get(key).value == "No"
    assert not bank.get("q_x").verified  # another application's answer never leaks


# --- classification ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("f", "key"),
    [
        (field("First Name", autocomplete="given-name"), "first_name"),
        (field("", name="job_application[last_name]"), "last_name"),
        (field("Email", kind="email"), "email"),
        (field("Mobile number"), "phone"),
        (field("LinkedIn Profile"), "linkedin"),
        (field("Resume/CV", kind="file"), "resume"),
        (field("Current location"), "location"),
        (field("Notice period"), "notice_period"),
        (field("Expected CTC"), "expected_salary"),
        (field("Are you willing to relocate?"), "willing_to_relocate"),
        (field("Are you legally authorized to work in India?"), "work_authorization"),
        (
            field("Are you authorized to work in the United States?"),
            "work_authorization_other_country",
        ),
        (field("Will you require visa sponsorship?"), "sponsorship"),
        (field("What is your citizenship?"), "citizenship"),
        (field("Gender"), "sensitive_demographic"),
        (field("Veteran status"), "sensitive_demographic"),
        (field("Disability status"), "sensitive_demographic"),
        (field("I agree to the privacy policy", kind="checkbox"), "consent"),
        (field("Security question: mother's maiden name"), "security_question"),
        (field("Have you ever been convicted of a crime?"), "legal_question"),
        (field("Password", kind="password"), "password"),
        (field("Code", autocomplete="one-time-code"), "otp"),
    ],
)
def test_classification(f: dict[str, Any], key: str) -> None:
    assert classify(f, "Gurugram, India")[0] == key


@pytest.mark.parametrize(
    ("f", "category"),
    [
        (field("Email", kind="email"), VERIFIED_AUTOFILL),
        (yes_no("Are you legally authorized to work in India?"), VERIFIED_AUTOFILL),
        (yes_no("Are you willing to relocate?", "radio"), VERIFIED_AUTOFILL),
        (field("Notice period"), VERIFIED_AUTOFILL),
        (field("Resume", "file"), VERIFIED_AUTOFILL),
        (field("Are you willing to work night shifts?"), UNKNOWN),
        (field("First Name"), UNKNOWN),  # three-word name: not split
        (field("What is your current CTC?"), UNKNOWN),
        (field("Are you willing to relocate to London?"), UNKNOWN),
        (field("Preferred work location"), UNKNOWN),
        (yes_no("Will you now or in the future require sponsorship?"), SENSITIVE),
        (yes_no("Are you legally authorized to work in the United States?"), SENSITIVE),
        (field("Gender", "select", options=[{"label": "Female"}]), SENSITIVE),
        (field("I accept the terms", "checkbox"), SENSITIVE),
        (field("What is your nationality?"), SENSITIVE),
        (field("Password", "password"), CHALLENGE),
        (field("Code", autocomplete="one-time-code"), CHALLENGE),
        (field("Expected salary", "number"), AMBIGUOUS),  # '10 LPA' is not converted
    ],
)
def test_categories(bank: AnswerBank, f: dict[str, Any], category: str) -> None:
    plan = plan_field(f, bank, "Gurugram, India", True)
    assert plan.category == category, plan
    assert (plan.action in ("fill", "upload_resume")) == (category == VERIFIED_AUTOFILL)


def test_only_verified_autofill_is_answered_optional_rest_is_skipped(bank: AnswerBank) -> None:
    optional = field("How did you hear about us?", required=False)
    assert plan_field(optional, bank, None, True).action == "skip"
    gender = field("Gender", "select", required=False, options=[{"label": "Female"}])
    plan = plan_field(gender, bank, None, True)
    assert plan.action == "skip" and plan.category == SENSITIVE


def test_sensitive_question_needs_the_users_own_answer(real_like: UserProfile) -> None:
    q = yes_no("Will you require sponsorship?")
    assert plan_field(q, AnswerBank(real_like), None, True).category == SENSITIVE
    row = ApplicationAnswer(
        application_id=None,
        question_key="sponsorship",
        question="q",
        answer="No",
        status=AnswerStatus.VERIFIED,
        source="answered by the user in the dashboard on 2026-09-28",
    )
    plan = plan_field(q, AnswerBank(real_like, [row]), None, True)
    assert plan.category == VERIFIED_AUTOFILL and plan.option == 2


def test_work_authorization_without_country_depends_on_job_location(bank: AnswerBank) -> None:
    q = yes_no("Are you legally authorized to work?")
    assert plan_field(q, bank, "Bengaluru, India", True).category == VERIFIED_AUTOFILL
    assert plan_field(q, bank, "New York, NY", True).category == SENSITIVE
    assert plan_field(q, bank, None, True).category == SENSITIVE


def test_verified_answers_pick_the_single_clear_option(bank: AnswerBank) -> None:
    plan = plan_field(yes_no("Are you legally authorized to work in India?"), bank, None, True)
    assert plan.option == 2 - 1 and plan.answer_status == "VERIFIED"  # "Yes"
    assert plan.source and "verified_answers" in plan.source


@pytest.mark.parametrize(
    ("options", "answer", "expected"),
    [
        (["Yes", "No"], "Yes - authorized to work in India", 0),
        (["Immediate", "15 days", "30 days"], "Immediate", 0),
        (["Select...", "No", "Yes"], "Yes", 2),
        (["Yes, within India", "Yes, anywhere", "No"], "Yes", None),  # ambiguous
        (["Authorized", "Not authorized"], "Yes - authorized to work in India", None),
        (["Gurugram", "Delhi"], "Gurugram (Gurgaon), India", None),  # closest-looking: no
        (["Immediately", "1 month"], "Immediate", None),  # similar is not the same
        (["Yes", "Yes"], "Yes", None),
    ],
)
def test_choose_option_never_picks_the_closest_looking(
    options: list[str], answer: str, expected: int | None
) -> None:
    assert choose_option(options, answer)[0] == expected


def test_dropdown_fields_are_resolved_in_the_browser(bank: AnswerBank) -> None:
    combo = field("Are you legally authorized to work in India?", "combobox")
    plan = plan_field(combo, bank, None, True)
    assert plan.category == VERIFIED_AUTOFILL and plan.option is None
    assert plan.value == "Yes - authorized to work in India"
    already = plan_field({**combo, "selected_label": "Yes"}, bank, None, True)
    assert already.selected == "Yes"


def test_resume_upload_needs_a_validated_pdf(bank: AnswerBank) -> None:
    assert plan_field(field("Resume", "file"), bank, None, True).action == "upload_resume"
    missing = plan_field(field("Resume", "file"), bank, None, False)
    assert missing.action == "block" and missing.category == UNKNOWN
