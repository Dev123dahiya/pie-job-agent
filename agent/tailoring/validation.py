"""Grounding validation: a tailored resume may only contain what the master resume and the
verified profile support. Every violation is reported; any violation rejects the resume.

Checks
- skills: each listed skill is verified (profile skills / project technologies) or written in
  the master resume; unconfirmed skills (e.g. Go) are always rejected
- technologies in prose (summary, bullets): same rule, so a requirement copied from the job
  description into a bullet is caught
- employers / titles / dates: every experience entry matches a verified role exactly
- projects: only verified projects, listing only technologies documented for that project
- education / certifications / publications: must match the profile or the master text
- metrics: every number in prose (percentages, counts, years) must occur in the evidence
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from agent.tailoring.experience import experience_claims
from agent.tailoring.schema import TailoredResume
from backend.core.profile import UserProfile

# Technologies commonly named in job posts; checked in free text even if not in the vocabulary.
COMMON_TECH = [
    "Kubernetes",
    "Terraform",
    "AWS",
    "GCP",
    "Azure",
    "Kafka",
    "Spark",
    "Hadoop",
    "Snowflake",
    "Databricks",
    "LangChain",
    "LangGraph",
    "LlamaIndex",
    "TensorFlow",
    "Keras",
    "PyTorch",
    "React",
    "Angular",
    "Vue",
    "Next.js",
    "Node.js",
    "Rust",
    "Scala",
    "Kotlin",
    "Swift",
    "Ruby",
    "PHP",
    "Golang",
    "Redis",
    "Elasticsearch",
    "GraphQL",
    "gRPC",
    "Airflow",
    "dbt",
    "Jenkins",
    "Ansible",
    "Helm",
    "Docker",
    "MongoDB",
    "PostgreSQL",
    "MySQL",
    "Cassandra",
    "DynamoDB",
    "Flink",
    "Hive",
    "Tableau",
    "Looker",
    "SageMaker",
    "Vertex AI",
    "Pinecone",
    "FAISS",
    "Java",
    "Python",
    "TypeScript",
    "JavaScript",
    "C++",
    "C#",
    "SQL",
    "Power BI",
    "Linux",
]

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}
_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?:\s?%|\+)?")
_GO = [re.compile(r"(?<![\w.#+-])Go(?![\w+#-])"), re.compile(r"\bgolang\b", re.I)]


def norm(text: str) -> str:
    text = text.replace("–", "-").replace("—", "-").replace("∼", "~")
    return " ".join(text.lower().split())


def key(text: str) -> str:
    """Comparable form of a skill/name: 'Node.js' == 'NodeJS', 'CI/CD' == 'CICD'."""
    t = text.lower().replace(".js", "js")
    return re.sub(r"[^a-z0-9+#]", "", t)


def _number(token: str) -> str:
    try:
        return format(Decimal(token).normalize(), "f")
    except InvalidOperation:
        return token


def numbers_in(text: str) -> set[str]:
    return {_number(m.group(1)) for m in _NUMBER.finditer(text)}


def _month(value: str) -> str | None:
    """Normalize 'May 2026', '2026-05', '05/2026' to '2026-05'; '', 'present' -> 'present'."""
    v = value.strip().lower()
    if v in ("", "present", "current", "now", "none"):
        return "present"
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})", v):
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    if m := re.fullmatch(r"(\d{1,2})/(\d{4})", v):
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    if m := re.fullmatch(r"([a-z]{3})[a-z]*\.?\s+(\d{4})", v):
        month = _MONTHS.get(m.group(1))
        return f"{m.group(2)}-{month:02d}" if month else None
    if re.fullmatch(r"\d{4}", v):
        return v
    return None


def _has_phrase(text_norm: str, phrase: str) -> bool:
    p = re.escape(norm(phrase))
    return re.search(rf"(?<![a-z0-9]){p}(?![a-z0-9])", text_norm) is not None


@dataclass
class Evidence:
    master_text: str
    profile: UserProfile
    vocabulary: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        facts = self.profile.verified_profile
        self.master_norm = norm(self.master_text)
        self.verified_keys = {key(s) for s in facts.all_skills()}
        self.unconfirmed_keys = {key(s) for s in facts.unconfirmed_skills}
        if "go" in self.unconfirmed_keys:
            self.unconfirmed_keys.add("golang")
        profile_text = " ".join(
            [str(facts.total_experience_years), *(e.grade or "" for e in facts.education)]
            + [str(e.graduation_year or "") for e in facts.education]
            + [h for w in facts.work_experience for h in w.highlights]
            + [p.description for p in facts.projects]
        )
        self.numbers = numbers_in(self.master_text) | numbers_in(profile_text)
        self.corpus_norm = self.master_norm + " " + norm(profile_text)

    def skill_supported(self, skill: str) -> bool:
        k = key(skill)
        return k in self.verified_keys or _has_phrase(self.master_norm, skill)

    def is_unconfirmed(self, skill: str) -> bool:
        return key(skill) in self.unconfirmed_keys


@dataclass
class Violation:
    code: str  # UNCONFIRMED_SKILL | UNSUPPORTED_SKILL | UNSUPPORTED_TECHNOLOGY | ...
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


def _mentions_go(text: str) -> bool:
    return any(p.search(text) for p in _GO)


def validate_tailored(resume: TailoredResume, evidence: Evidence) -> list[Violation]:
    v: list[Violation] = []
    facts = evidence.profile.verified_profile

    # --- skills ---
    for item in resume.all_skill_items():
        if evidence.is_unconfirmed(item) or _mentions_go(item):
            v.append(
                Violation("UNCONFIRMED_SKILL", f"'{item}' is unconfirmed and cannot be claimed")
            )
        elif not evidence.skill_supported(item):
            v.append(
                Violation(
                    "UNSUPPORTED_SKILL", f"'{item}' is not in the master resume or verified skills"
                )
            )

    # --- technologies and Go in prose ---
    vocab = {t for t in [*COMMON_TECH, *evidence.vocabulary] if len(key(t)) > 1}
    for text in resume.free_text():
        if _mentions_go(text):
            v.append(Violation("UNCONFIRMED_SKILL", f"Mentions Go (unconfirmed): '{text[:80]}'"))
        text_norm = norm(text)
        for term in sorted(vocab):
            if not _has_phrase(text_norm, term):
                continue
            if evidence.is_unconfirmed(term):
                v.append(Violation("UNCONFIRMED_SKILL", f"Mentions unconfirmed '{term}'"))
            elif not evidence.skill_supported(term):
                v.append(
                    Violation(
                        "UNSUPPORTED_TECHNOLOGY",
                        f"Mentions '{term}', which the master resume does not support",
                    )
                )

    # --- experience: employers, titles, dates ---
    roles = {key(w.company): w for w in facts.work_experience}
    seen: set[str] = set()
    for entry in resume.experience:
        role = roles.get(key(entry.company))
        if role is None:
            v.append(
                Violation("UNSUPPORTED_EMPLOYER", f"'{entry.company}' is not a verified employer")
            )
            continue
        if key(entry.company) in seen:
            v.append(Violation("DUPLICATE_ROLE", f"'{entry.company}' listed twice"))
        seen.add(key(entry.company))
        if norm(entry.title) != norm(role.title):
            v.append(
                Violation(
                    "UNSUPPORTED_TITLE",
                    f"Title '{entry.title}' at {role.company} must be '{role.title}'",
                )
            )
        if _month(entry.start_date) != _month(role.start_date) or _month(entry.end_date) != _month(
            role.end_date or ""
        ):
            v.append(
                Violation(
                    "UNSUPPORTED_DATES",
                    f"Dates for {role.company} must be {role.start_date} to "
                    f"{role.end_date or 'present'}, got {entry.start_date} to "
                    f"{entry.end_date or 'present'}",
                )
            )

    # --- projects ---
    projects = {key(p.name): p for p in facts.projects}
    for proj in resume.projects:
        match = projects.get(key(proj.name)) or next(
            (
                p
                for k, p in projects.items()
                if key(proj.name).startswith(k) or k.startswith(key(proj.name))
            ),
            None,
        )
        if match is None:
            v.append(Violation("UNSUPPORTED_PROJECT", f"'{proj.name}' is not a verified project"))
            continue
        # Documented for THIS project: its technology list or its own description.
        allowed = {key(t) for t in match.technologies}
        description = norm(match.description)
        for tech in proj.technologies:
            if evidence.is_unconfirmed(tech) or _mentions_go(tech):
                v.append(Violation("UNCONFIRMED_SKILL", f"'{tech}' on {match.name} is unconfirmed"))
            elif key(tech) not in allowed and not _has_phrase(description, tech):
                v.append(
                    Violation(
                        "UNSUPPORTED_TECHNOLOGY",
                        f"'{tech}' is not documented for project {match.name}",
                    )
                )

    # --- education ---
    for edu in resume.education:
        verified = next(
            (
                e
                for e in facts.education
                if key(e.institution) in key(edu.institution)
                or key(edu.institution) in key(e.institution)
            ),
            None,
        )
        if verified is None:
            v.append(Violation("UNSUPPORTED_EDUCATION", f"'{edu.institution}' is not verified"))
            continue
        for label, value in (("degree", edu.degree), ("field", edu.field_of_study)):
            if value and not (
                _has_phrase(evidence.corpus_norm, value)
                or norm(value) in {norm(verified.degree), norm(verified.field_of_study or "")}
            ):
                v.append(Violation("UNSUPPORTED_EDUCATION", f"{label} '{value}' is not verified"))
        if edu.graduation_year and edu.graduation_year.strip() != str(
            verified.graduation_year or ""
        ):
            v.append(
                Violation(
                    "UNSUPPORTED_DATES", f"Graduation year '{edu.graduation_year}' is not verified"
                )
            )
        if edu.grade and not numbers_in(edu.grade) <= evidence.numbers:
            v.append(Violation("UNSUPPORTED_EDUCATION", f"Grade '{edu.grade}' is not verified"))

    # --- certifications / publications ---
    verified_certs = {norm(c) for c in facts.certifications}
    for cert in resume.certifications:
        if norm(cert) not in verified_certs:
            v.append(
                Violation("UNSUPPORTED_CERTIFICATION", f"'{cert}' is not a verified certification")
            )
    for pub in resume.publications:
        title = re.split(r"\s[|(]", pub)[0]
        if not _has_phrase(evidence.corpus_norm, title):
            v.append(
                Violation("UNSUPPORTED_PUBLICATION", f"'{title[:80]}' is not in the master resume")
            )

    # --- metrics / numbers in prose ---
    for text in resume.free_text():
        for number in numbers_in(text) - evidence.numbers:
            v.append(
                Violation(
                    "UNSUPPORTED_METRIC",
                    f"Number '{number}' in '{text[:80]}' is not in the master resume",
                )
            )

    # --- years of experience: never more than the verified full-time total ---
    have = float(facts.total_experience_years)
    for text in resume.free_text():
        for phrase, years in experience_claims(text):
            if years > have:
                v.append(
                    Violation(
                        "UNSUPPORTED_EXPERIENCE",
                        f"'{phrase}' exceeds verified full-time experience ({have:g} years)",
                    )
                )

    # De-duplicate while keeping order.
    unique: dict[tuple[str, str], Violation] = {(x.code, x.detail): x for x in v}
    return list(unique.values())


def validation_report(violations: list[Violation]) -> dict[str, Any]:
    return {
        "passed": not violations,
        "violations": [x.as_dict() for x in violations],
        "checks": [
            "skills",
            "technologies_in_text",
            "unconfirmed_skills",
            "employers",
            "titles",
            "dates",
            "projects",
            "education",
            "certifications",
            "publications",
            "metrics",
            "years_of_experience",
        ],
    }
