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


class AvailableAnswer(ToolOutput):
    key: str  # the exact key fill_application_field accepts
    status: str  # VERIFIED | UNCONFIRMED
    source: str  # where it comes from (profile, identity, the user's saved answer)


class AnswersOutput(ToolOutput):
    answers: list[AvailableAnswer]
    missing: list[MissingAnswer]
    note: str


def get_application_answers(ctx: ToolContext, _: NoInput) -> AnswersOutput:
    """Which answers exist, never what they are (REQUIREMENTS 13). The form filler looks the
    values up itself at fill time; the agent only ever passes answer keys. Built from the same
    AnswerBank the filler uses, so the key names always match."""
    from sqlalchemy import select

    from agent.execution.answers import AnswerBank
    from database.models import ApplicationAnswer

    saved = ctx.db.scalars(
        select(ApplicationAnswer).where(ApplicationAnswer.application_id.is_(None))
    ).all()  # the user's answers for every application
    bank = AnswerBank(ctx.profile, list(saved))
    answers = [
        AvailableAnswer(key=a.key, status=a.status.value, source=a.source)
        for a in bank.all()
        if a.status.value in ("VERIFIED", "UNCONFIRMED")
    ]
    verified = set(bank.verified_keys())
    missing = [
        MissingAnswer(key=k, question=q) for k, q in STANDARD_QUESTIONS.items() if k not in verified
    ]
    return AnswersOutput(
        answers=answers,
        missing=missing,
        note="Only VERIFIED answers are ever filled in, by key; their values are not shown. "
        "Anything missing or UNCONFIRMED must be asked of the user (BLOCKED_NEEDS_USER), "
        "never guessed.",
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
        description="List which answers exist for application forms (answer keys and whether "
        "they are verified; values are never shown) and the common questions that have NO "
        "verified answer.",
        permission=ToolPermission.READ_ONLY,
        input_model=NoInput,
        output_model=AnswersOutput,
        handler=get_application_answers,
    ),
]
