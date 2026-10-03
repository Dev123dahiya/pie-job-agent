"""Application answer bank: every value PIE may type into an employer's form.

Each answer has a status. Only VERIFIED answers are ever entered automatically; UNCONFIRMED
and UNKNOWN answers block the application until the user answers (BLOCKED_NEEDS_USER).

Sources, in order of precedence:
1. answers the user gave in the dashboard for THIS application (application_answers table)
2. answers the user gave for all applications (application_answers, application_id NULL)
3. the verified profile: identity (name, first/last name), contact details, links and
   verified_answers (work authorization in India, relocation, notice period, current CTC,
   expected salary); total_experience comes from total_experience_years

Nothing is ever guessed:
- first/last name come from the verified identity: explicit first_name/last_name, or a name
  of exactly two words. "Asha Rani Verma" is three words, so the split is UNCONFIRMED.
- if two sources disagree (identity vs verified_answers), the answer is marked as a
  conflict and treated as AMBIGUOUS - PIE does not pick one.
- nothing is stored or inferred for sponsorship, citizenship, demographics, security or
  country-specific legal questions; those are asked of the user.
"""

import hashlib
import re
from dataclasses import dataclass

from backend.core.profile import UserProfile
from database.enums import AnswerStatus
from database.models import ApplicationAnswer

PROFILE_SOURCE = "config/user_profile.yaml (verified_profile)"
IDENTITY_SOURCE = "config/user_profile.yaml (verified_profile identity)"
ANSWERS_SOURCE = "config/user_profile.yaml (verified_answers)"


@dataclass(frozen=True)
class Answer:
    key: str
    value: str | None
    status: AnswerStatus
    source: str
    note: str | None = None
    conflict: bool = False  # two verified sources disagree: never auto-filled

    @property
    def verified(self) -> bool:
        return self.status is AnswerStatus.VERIFIED and bool(self.value) and not self.conflict

    @property
    def from_user(self) -> bool:
        return self.source.startswith("answered by the user")


def question_key(question: str) -> str:
    """Stable key for a question the profile does not cover (e.g. 'q_night_shifts_1a2b3c4d')."""
    text = re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()
    slug = "_".join(text.split()[:8])[:60] or "question"
    return f"q_{slug}_{hashlib.sha1(text.encode()).hexdigest()[:8]}"


def _identity_names(profile: UserProfile) -> dict[str, tuple[str | None, str | None]]:
    """first/last name from the verified identity: (value, note-if-not-verified)."""
    facts = profile.verified_profile
    if facts.first_name and facts.last_name:
        return {"first_name": (facts.first_name, None), "last_name": (facts.last_name, None)}
    words = facts.name.split()
    if len(words) == 2:
        return {"first_name": (words[0], None), "last_name": (words[1], None)}
    note = (
        f"Your verified name has {len(words)} words; PIE will not decide where the first name "
        "ends. Add first_name/last_name to verified_profile, or answer here."
    )
    return {"first_name": (None, note), "last_name": (None, note)}


class AnswerBank:
    def __init__(
        self,
        profile: UserProfile,
        stored: list[ApplicationAnswer] | None = None,
        application_id: object | None = None,
    ) -> None:
        facts = profile.verified_profile
        answers: dict[str, Answer] = {}

        def put(key: str, value: object, source: str = PROFILE_SOURCE) -> None:
            if value in (None, ""):
                return
            text = str(value).strip()
            existing = answers.get(key)
            if existing and existing.value and existing.value != text:
                answers[key] = Answer(
                    key, None, AnswerStatus.UNCONFIRMED, f"{existing.source} vs {source}",
                    f"Conflicting verified values ('{existing.value}' vs '{text}')", True,
                )  # fmt: skip
                return
            answers[key] = Answer(key, text, AnswerStatus.VERIFIED, source)

        put("full_name", facts.name, IDENTITY_SOURCE)
        for key, (value, note) in _identity_names(profile).items():
            if value is not None:
                put(key, value, IDENTITY_SOURCE)
            else:
                answers[key] = Answer(key, None, AnswerStatus.UNCONFIRMED, IDENTITY_SOURCE, note)
        put("email", facts.email)
        put("phone", facts.phone)
        put("location", facts.location)
        if "total_experience_years" in facts.model_fields_set:  # set by the user, not a default
            put("total_experience", f"{facts.total_experience_years:g}")
        for name, url in facts.links.items():
            put(name.lower(), url)
        for key, value in facts.verified_answers.items():
            current = answers.get(key)
            if (
                key in ("first_name", "last_name")
                and current is not None
                and current.status is AnswerStatus.UNCONFIRMED
                and not current.conflict
            ):
                # The user stated the name part explicitly; the identity could not split it.
                answers[key] = Answer(
                    key, str(value).strip(), AnswerStatus.VERIFIED, ANSWERS_SOURCE
                )
                continue
            put(key, value, ANSWERS_SOURCE)
        # User-supplied answers: global first, then application-specific (wins).
        for row in sorted(stored or [], key=lambda r: r.application_id is not None):
            if row.application_id not in (None, application_id):
                continue
            answers[row.question_key] = Answer(row.question_key, row.answer, row.status, row.source)
        self._answers = answers

    def get(self, key: str) -> Answer:
        return self._answers.get(key) or Answer(
            key, None, AnswerStatus.UNKNOWN, "none", "No verified answer"
        )

    def all(self) -> list["Answer"]:
        """Every answer the bank knows (any status), sorted by key."""
        return sorted(self._answers.values(), key=lambda a: a.key)

    def verified_keys(self) -> list[str]:
        return sorted(k for k, a in self._answers.items() if a.verified)
