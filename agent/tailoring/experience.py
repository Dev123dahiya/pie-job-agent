"""Experience gap: the job's required experience vs the user's VERIFIED full-time experience.

A factual constraint, computed deterministically. Tailoring can improve how documented
qualifications are presented; it cannot change this, and it is never hidden or softened.

- candidate experience = ``verified_profile.total_experience_years`` (the profile records
  full-time experience there; internships are NOT counted as full-time experience)
- required experience = the posting's ``experience_required``, else an explicit
  "N years ... experience" line in its requirements. Anything else -> gap "unknown".
"""

import re
from typing import Any, Literal

from backend.core.profile import UserProfile

GapStatus = Literal["gap", "meets", "exceeds", "unknown"]

SCOPE_NOTE = (
    "Experience gap is a factual constraint and is never concealed. Resume tailoring only "
    "improves the relevance of documented qualifications; shortlist eligibility is decided "
    "separately by the job-analysis rules. Internships are not counted as full-time experience."
)

_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}  # fmt: skip
_NUM = r"(\d+(?:\.\d+)?|" + "|".join(_WORDS) + r")"
_UNIT = r"(years?|yrs?|months?)"
# "2-5 years", "2 to 5 yrs", "3+ years", "6 months", "minimum of 2 years"
_RANGE = re.compile(rf"{_NUM}\s*\+?\s*(?:-|–|—|to)\s*{_NUM}\s*\+?\s*{_UNIT}\b", re.IGNORECASE)
_SINGLE = re.compile(rf"{_NUM}\s*(?:\+|or more|plus)?\s*{_UNIT}\b", re.IGNORECASE)
_ENTRY = re.compile(r"\b(fresher|freshers|new grad(uate)?s?|entry[- ]level|no experience)\b", re.I)
_INTERN = re.compile(r"\bintern(ship)?\b", re.IGNORECASE)


def _value(token: str) -> float:
    token = token.lower()
    return float(_WORDS[token]) if token in _WORDS else float(token)


def _years(value: float, unit: str) -> float:
    return value / 12 if unit.lower().startswith("month") else value


def parse_required_years(text: str | None) -> tuple[float, float | None] | None:
    """(min_years, max_years or None) stated in ``text``; None when it states no amount."""
    if not text:
        return None
    if m := _RANGE.search(text):
        low, high = _years(_value(m[1]), m[3]), _years(_value(m[2]), m[3])
        return (min(low, high), max(low, high))
    if m := _SINGLE.search(text):
        return (_years(_value(m[1]), m[2]), None)
    if _ENTRY.search(text):
        return (0.0, None)
    return None


def _requirement_text(job: dict[str, Any]) -> tuple[str | None, str | None]:
    stated = (job.get("experience_required") or "").strip()
    if stated:
        return stated, "job.experience_required"
    for line in job.get("requirements") or []:
        if re.search(r"experience", line, re.I) and parse_required_years(line) is not None:
            return line.strip(), "job.requirements"
    return None, None


def _fmt_years(value: float) -> str:
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{text} year" + ("" if text == "1" else "s")


def is_internship(title: str) -> bool:
    return _INTERN.search(title) is not None


def experience_gap(profile: UserProfile, job: dict[str, Any]) -> dict[str, Any]:
    """Compare required vs verified full-time experience. Never rounds the gap away."""
    facts = profile.verified_profile
    have = float(facts.total_experience_years)
    internships = [
        f"{w.title} at {w.company} ({w.start_date} - {w.end_date or 'present'})"
        for w in facts.work_experience
        if is_internship(w.title)
    ]
    required, source = _requirement_text(job)
    parsed = parse_required_years(required)
    result: dict[str, Any] = {
        "required": required,
        "required_source": source,
        "required_min_years": None,
        "required_max_years": None,
        "candidate": _fmt_years(have),
        "candidate_years": have,
        "candidate_basis": "Verified full-time experience "
        "(verified_profile.total_experience_years)",
        "internships": internships,
        "internships_counted_as_full_time": False,
        "shortfall_years": None,
        "status": "unknown",
        "gap": "unknown",
        "note": SCOPE_NOTE,
    }
    if parsed is None:
        return result
    low, high = parsed
    result["required_min_years"], result["required_max_years"] = low, high
    if have < low:
        result.update(status="gap", gap=True, shortfall_years=round(low - have, 2))
    elif high is not None and have > high:
        result.update(status="exceeds", gap=False)
    else:
        result.update(status="meets", gap=False)
    return result


_CLAIM = re.compile(
    rf"{_NUM}\s*\+?\s*(?:(?:-|–|to)\s*{_NUM}\s*\+?\s*)?{_UNIT}(?:\s+\w+){{0,3}}?\s+(?:of\s+)?"
    r"(?:\w+\s+){0,2}experience",
    re.IGNORECASE,
)


def experience_claims(text: str) -> list[tuple[str, float]]:
    """'N years of (professional) experience' claims in prose, with the claimed years."""
    claims = []
    for m in _CLAIM.finditer(text):
        top = m[2] or m[1]
        claims.append((m[0], _years(_value(top), m[3])))
    return claims
