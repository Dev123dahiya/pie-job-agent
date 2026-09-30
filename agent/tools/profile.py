"""Profile tools. Verified facts and preferences stay separate; unconfirmed skills are
reported as such and are never part of the verified profile."""

from typing import Any

from agent.tools.base import NoInput, Tool, ToolContext, ToolOutput
from backend.core.profile import UserProfile
from backend.services.readiness import STANDARD_QUESTIONS
from database.enums import ToolPermission


def verified_profile_view(profile: UserProfile) -> dict[str, Any]:
    """Facts only. Contact details and unconfirmed skills are deliberately excluded."""
    facts = profile.verified_profile
    view = facts.model_dump(
        mode="json",
        exclude={"email", "phone", "verified_answers", "unconfirmed_skills"},
    )
    view["skills"] = facts.confirmed_skills()
    return view


class ProfileOutput(ToolOutput):
    verified_profile: dict[str, Any]
    unconfirmed_skills: list[str]
    unconfirmed_note: str
    preferences: dict[str, Any]


def get_user_profile(ctx: ToolContext, _: NoInput) -> ProfileOutput:
    profile = ctx.profile
    return ProfileOutput(
        verified_profile=verified_profile_view(profile),
        unconfirmed_skills=profile.verified_profile.unconfirmed_skills,
        unconfirmed_note=(
            "Unconfirmed skills are NOT verified. Never claim them, use them to match jobs, "
            "or put them in any application."
        ),
        preferences=profile.preferences.model_dump(mode="json"),
    )


class MissingAnswer(ToolOutput):
    key: str
    question: str


class AnswersOutput(ToolOutput):
    answers: dict[str, str]
    missing: list[MissingAnswer]
    note: str


def get_application_answers(ctx: ToolContext, _: NoInput) -> AnswersOutput:
    facts = ctx.profile.verified_profile
    answers: dict[str, str] = {"full_name": facts.name, "email": str(facts.email)}
    if facts.phone:
        answers["phone"] = facts.phone
    if facts.location:
        answers["current_location"] = facts.location
    for key, url in facts.links.items():
        answers[f"link_{key}"] = url
    answers.update(facts.verified_answers)
    missing = [
        MissingAnswer(key=k, question=q) for k, q in STANDARD_QUESTIONS.items() if k not in answers
    ]
    return AnswersOutput(
        answers=answers,
        missing=missing,
        note="Only these verified answers may be used. Anything missing must be asked of the "
        "user (BLOCKED_NEEDS_USER), never guessed.",
    )


TOOLS = [
    Tool(
        name="get_user_profile",
        description="Read the user's VERIFIED profile (facts), unconfirmed skills (not facts) "
        "and job preferences (wishes, not facts). Contact details are not included.",
        permission=ToolPermission.READ_ONLY,
        input_model=NoInput,
        output_model=ProfileOutput,
        handler=get_user_profile,
    ),
    Tool(
        name="get_application_answers",
        description="Read verified answers for application forms (name, contact, notice period, "
        "expected salary...) and the list of common questions that have NO verified answer.",
        permission=ToolPermission.READ_ONLY,
        input_model=NoInput,
        output_model=AnswersOutput,
        handler=get_application_answers,
    ),
]
