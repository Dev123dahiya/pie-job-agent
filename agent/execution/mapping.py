"""Field classification and mapping: which verified answer (if any) belongs in each field.

Deterministic. Uses the label, name, id, autocomplete, placeholder and surrounding text - not
the label alone. Every field is put in exactly one category:

    VERIFIED_AUTOFILL        a VERIFIED answer (or the validated resume) fits unambiguously
    UNKNOWN                  PIE does not know the answer (or only an unconfirmed one)
    AMBIGUOUS                an answer exists but does not map to exactly one value/option
    SENSITIVE_USER_REQUIRED  sponsorship, citizenship, demographics, consent, security or
                             country-specific legal questions - only the user answers these
    CHALLENGE                password / one-time code: never handled by PIE

Only VERIFIED_AUTOFILL is ever filled automatically. A required field in any other category
blocks the application (BLOCKED_NEEDS_USER); an optional one is skipped. A sensitive question
the user has answered themselves in the dashboard becomes VERIFIED_AUTOFILL for that answer.

Materially different questions are never answered with a similar verified answer:
- "Will you require visa sponsorship?" is not the work-authorization question;
- "Are you authorized to work in the United States?" is not "authorized to work in India";
- "Willing to relocate to London?" is not a generic "willing to relocate";
- "Current CTC" is not "expected salary"; "When can you start?" is not "notice period".
"""

import re
from dataclasses import asdict, dataclass
from typing import Any

from agent.execution.answers import Answer, AnswerBank, question_key

VERIFIED_AUTOFILL = "VERIFIED_AUTOFILL"
UNKNOWN = "UNKNOWN"
AMBIGUOUS = "AMBIGUOUS"
SENSITIVE = "SENSITIVE_USER_REQUIRED"
CHALLENGE = "CHALLENGE"
CATEGORIES = (VERIFIED_AUTOFILL, UNKNOWN, AMBIGUOUS, SENSITIVE, CHALLENGE)

INDIA_PLACES = (
    "india", "gurgaon", "gurugram", "delhi", "new delhi", "ncr", "noida", "bangalore",
    "bengaluru", "mumbai", "pune", "hyderabad", "chennai", "kolkata", "ahmedabad", "jaipur",
)  # fmt: skip
OTHER_COUNTRIES = re.compile(
    r"united states|\busa?\b|u\.s\.|america|canada|united kingdom|\buk\b|britain|europe"
    r"|\beu\b|germany|france|ireland|netherlands|spain|singapore|australia|new zealand|japan"
    r"|china|uae|dubai|israel|switzerland|sweden|poland|mexico|brazil",
    re.I,
)
CHALLENGE_KEYS = {"password", "otp"}
# Only the user may answer these (a dashboard answer from the user is accepted).
SENSITIVE_KEYS = {
    "sensitive_demographic",
    "consent",
    "sponsorship",
    "citizenship",
    "security_question",
    "legal_question",
    "work_authorization_other_country",
}
TEXT_KINDS = {"text", "textarea", "email", "tel", "url"}


@dataclass
class FieldPlan:
    field_id: str
    label: str
    kind: str
    required: bool
    key: str | None  # the answer key this field needs
    category: str  # one of CATEGORIES
    action: str  # fill | upload_resume | skip | block
    reason: str
    answer_status: str | None = None
    source: str | None = None
    option: int | None = None  # native select / radio: the option index to choose
    value: str | None = None  # the verified value (text) or the answer to find (dropdowns)
    selected: str | None = None  # dropdown label already selected on the page

    def public(self) -> dict[str, Any]:
        return asdict(self)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_\-\[\]]+", " ", text or "")).strip().lower()


def field_text(field: dict[str, Any]) -> str:
    parts = [field.get(k) or "" for k in ("label", "name", "html_id", "placeholder")]
    text = _norm(" ".join(parts))
    if not _norm(field.get("label") or ""):
        text += " " + _norm(field.get("surrounding") or "")[:200]
    return text


def question_text(field: dict[str, Any]) -> str:
    q = (field.get("label") or field.get("placeholder") or field.get("name") or "").strip()
    return re.sub(r"\s*\*\s*$", "", q) or (field.get("surrounding") or "")[:200]


def _in_india(text: str) -> bool:
    return any(re.search(rf"\b{re.escape(p)}\b", text) for p in INDIA_PLACES)


def classify(field: dict[str, Any], job_location: str | None) -> tuple[str | None, str]:
    """(answer key, reason). None = PIE does not know what this field asks."""
    kind = field.get("kind") or "text"
    text = field_text(field)
    auto = (field.get("autocomplete") or "").lower()

    if kind == "password":
        return "password", "password field (PIE never handles passwords)"
    if auto == "one-time-code" or re.search(r"\botp\b|verification code|one time code", text):
        return "otp", "one-time code field"
    if kind == "file":
        if re.search(r"cover ?letter", text):
            return "cover_letter", "cover letter upload"
        if re.search(r"resume|\bcv\b|curriculum", text):
            return "resume", "resume upload"
        return None, "unrecognised file upload"
    if re.search(
        r"gender|\brace\b|ethnic|veteran|disabilit|sexual orientation|pronoun|religio|caste"
        r"|date of birth|\bage\b|marital|hispanic|latino",
        text,
    ):
        return "sensitive_demographic", "demographic / sensitive question"
    if kind in ("checkbox", "checkbox_group") and re.search(
        r"agree|consent|terms|privacy|acknowledge|certify|confirm that|accept", text
    ):
        return "consent", "consent / agreement"
    if re.search(r"security question|mother'?s maiden|first pet|favou?rite", text):
        return "security_question", "security question"
    if re.search(r"citizen|nationality|passport", text):
        return "citizenship", "citizenship / nationality"
    if re.search(
        r"criminal|convicted|felony|background check|security clearance|export control"
        r"|non[- ]?compete|government official|politically exposed",
        text,
    ):
        return "legal_question", "legal / compliance question"

    # Consequential questions - strict.
    if re.search(r"sponsor|visa", text):
        return "sponsorship", "sponsorship question (separate from work authorization)"
    if re.search(
        r"authori[sz]ed to work|work authori[sz]ation|right to work|eligible to work"
        r"|legally (allowed|permitted) to work",
        text,
    ):
        if _in_india(text):
            return "work_authorization", "work authorization in India"
        if country := OTHER_COUNTRIES.search(text):
            return "work_authorization_other_country", f"work authorization for {country.group(0)}"
        if job_location and _in_india(_norm(job_location)):
            return "work_authorization", "work authorization; job is located in India"
        return (
            "work_authorization_other_country",
            "work authorization for an unspecified country (job not in India)",
        )
    if re.search(r"relocat", text):
        place = re.search(r"relocat\w* to ([a-z .,]+)", text)
        if (
            place
            and not _in_india(place.group(1))
            and not re.search(
                r"this (location|city|office)|the (job|role|office) location", place.group(1)
            )
        ):
            return None, f"relocation to {place.group(1).strip()} (not verified)"
        return "willing_to_relocate", "willingness to relocate"
    if re.search(r"notice period", text):
        return "notice_period", "notice period"
    if re.search(r"current (salary|ctc|compensation)", text):
        return None, "current salary (not verified)"
    if re.search(
        r"expected (salary|ctc|compensation)|salary expectation|desired (salary|pay)"
        r"|compensation expectation|\bsalary\b|\bctc\b",
        text,
    ):
        return "expected_salary", "expected salary"

    # Identity and contact details.
    by_autocomplete = {
        "given-name": "first_name",
        "family-name": "last_name",
        "name": "full_name",
        "email": "email",
        "tel": "phone",
        "tel-national": "phone",
    }
    if auto in by_autocomplete:
        return by_autocomplete[auto], f"autocomplete={auto}"
    if kind == "email" or re.search(r"e ?mail", text):
        return "email", "email"
    if kind == "tel" or re.search(r"phone|mobile|telephone|contact number", text):
        return "phone", "phone"
    if re.search(r"preferred (first )?name|nick ?name", text):
        return None, "preferred name (not verified)"
    if re.search(r"first name|given name|\bfname\b|firstname", text):
        return "first_name", "first name"
    if re.search(r"last name|surname|family name|\blname\b|lastname", text):
        return "last_name", "last name"
    if re.search(r"full name|^name\b|your name|legal name", text):
        return "full_name", "full name"
    if re.search(r"linkedin", text):
        return "linkedin", "LinkedIn URL"
    if re.search(r"github", text):
        return "github", "GitHub URL"
    if re.search(r"portfolio|personal (web)?site|website", text):
        return "portfolio", "portfolio / website"
    if re.search(
        r"current location|\blocation\b|\bcity\b|where are you (based|located)", text
    ) and not re.search(r"prefer|desired|willing|open to", text):
        return "location", "current location"
    return None, "not recognised"


def choose_option(options: list[str], answer: str) -> tuple[int | None, str]:
    """Index of the ONE option that clearly matches a verified answer - never the closest-looking.

    Clear matches: the same text (ignoring case/spacing), or for a yes/no answer ("Yes - ...")
    the single option that is exactly "Yes"/"No" or starts with it. Anything else: None."""
    cleaned = [(i, _norm(o)) for i, o in enumerate(options)]
    cleaned = [(i, o) for i, o in cleaned if o and not re.match(r"^(select|choose|--|please)", o)]
    target = _norm(answer)
    exact = [i for i, o in cleaned if o == target]
    if len(exact) == 1:
        return exact[0], "exact option match"
    if len(exact) > 1:
        return None, f"{len(exact)} identical options - ambiguous"
    polarity = re.match(r"^(yes|no)\b", target)
    if polarity:
        word = polarity.group(1)
        matches = [i for i, o in cleaned if re.match(rf"^{word}\b", o)]
        if len(matches) == 1:
            return matches[0], f"the only option starting with '{word}'"
        if matches:
            return None, f"{len(matches)} options start with '{word}' - ambiguous"
        return None, f"no option says '{word}'"
    return None, "no option exactly matches the verified answer"


def plan_field(
    field: dict[str, Any], bank: AnswerBank, job_location: str | None, resume_ready: bool
) -> FieldPlan:
    kind = field.get("kind") or "text"
    required = bool(field.get("required"))
    label = question_text(field)
    key, reason = classify(field, job_location)
    base = {"field_id": field["id"], "label": label, "kind": kind, "required": required}

    def stop(
        category: str, why: str, k: str | None = key, answer: Answer | None = None
    ) -> FieldPlan:
        status = answer.status.value if answer else "UNKNOWN"
        action = "block" if required else "skip"
        return FieldPlan(**base, key=k, category=category, action=action,
                         reason=why if required else f"optional: {why}",
                         answer_status=status)  # fmt: skip

    if key in CHALLENGE_KEYS:
        return FieldPlan(**base, key=key, category=CHALLENGE, action="block", reason=reason)
    if key == "resume":
        if resume_ready:
            return FieldPlan(**base, key=key, category=VERIFIED_AUTOFILL, action="upload_resume",
                             reason=reason, answer_status="VERIFIED",
                             source="validated tailored resume")  # fmt: skip
        return stop(UNKNOWN, "no validated tailored resume PDF")
    if key == "cover_letter":
        return stop(UNKNOWN, "PIE does not write cover letters")
    if key is None:
        # Unknown question: only an answer the user gave for exactly this question counts.
        stored = bank.get(question_key(label))
        if not stored.verified:
            return stop(UNKNOWN, f"no verified answer ({reason})", question_key(label), stored)
        key = stored.key

    answer = bank.get(key)
    if key in SENSITIVE_KEYS and not (answer.verified and answer.from_user):
        return stop(SENSITIVE, f"{reason}: only you can answer this", key, answer)
    if answer.conflict:
        return stop(AMBIGUOUS, f"answer '{key}': {answer.note}", key, answer)
    if not answer.verified:
        note = f": {answer.note}" if answer.note else ""
        return stop(UNKNOWN, f"answer '{key}' is {answer.status.value}{note}", key, answer)
    assert answer.value is not None

    plan = FieldPlan(**base, key=key, category=VERIFIED_AUTOFILL, action="fill", reason=reason,
                     answer_status=answer.status.value, source=answer.source,
                     value=answer.value, selected=field.get("selected_label") or None)  # fmt: skip
    if kind in ("select", "radio"):
        labels = [o.get("label") or o.get("value") or "" for o in field.get("options") or []]
        option, why = choose_option(labels, answer.value)
        if option is None:
            return stop(AMBIGUOUS, f"verified answer '{answer.value}' but {why}", key, answer)
        plan.option = option
        plan.reason += f"; {why}"
        return plan
    if kind == "combobox":
        # Options appear only when the dropdown is opened: the browser step searches for the
        # value and selects it only on a clear match (otherwise the field blocks).
        plan.reason += "; option chosen in the dropdown only on a clear match"
        return plan
    if kind == "checkbox":
        if not re.match(r"^(yes|no)\b", _norm(answer.value)):
            return stop(AMBIGUOUS, "checkbox needs a yes/no answer", key, answer)
        plan.value = "true" if _norm(answer.value).startswith("yes") else "false"
        return plan
    if kind in ("checkbox_group", "date"):
        return stop(AMBIGUOUS, f"{kind} controls are not filled automatically", key, answer)
    if kind == "number" and not re.fullmatch(r"-?\d+(\.\d+)?", answer.value.strip()):
        return stop(
            AMBIGUOUS,
            f"numeric field but the verified answer is '{answer.value}' (not converting)",
            key,
            answer,
        )
    return plan


def plan_fields(
    fields: list[dict[str, Any]],
    bank: AnswerBank,
    job_location: str | None,
    resume_ready: bool,
) -> list[FieldPlan]:
    return [plan_field(f, bank, job_location, resume_ready) for f in fields]
