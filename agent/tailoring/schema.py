"""The structured tailored resume Claude returns (header/contact are NOT part of it: PIE fills
those from the verified profile, so they can never be altered)."""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

TailoringStrategy = Literal["balanced", "skills_first", "projects_first", "experience_first"]
RequestedFormat = Literal["pdf", "markdown"]

STRATEGY_GUIDANCE: dict[str, str] = {
    "balanced": "Order sections by relevance to this job; keep the resume to one page.",
    "skills_first": "Lead with the most relevant verified skills; then experience and projects.",
    "projects_first": "Lead with the most relevant projects (early-career profile), then "
    "experience.",
    "experience_first": "Lead with the most relevant work experience; then projects and skills.",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkillGroup(_Strict):
    category: str = Field(min_length=1, max_length=60)
    items: list[str] = Field(max_length=40)


class ExperienceEntry(_Strict):
    company: str
    title: str
    start_date: str
    end_date: str  # "" for current roles
    location: str
    bullets: list[str] = Field(max_length=8)


class ProjectEntry(_Strict):
    name: str
    technologies: list[str] = Field(max_length=15)
    bullets: list[str] = Field(max_length=6)


class EducationEntry(_Strict):
    institution: str
    degree: str
    field_of_study: str
    graduation_year: str
    grade: str


class TailoredResume(_Strict):
    section_order: list[
        Literal["summary", "skills", "experience", "projects", "education", "publications"]
    ] = Field(min_length=1, max_length=6)
    summary: str = Field(max_length=900)
    skills: list[SkillGroup] = Field(max_length=10)
    experience: list[ExperienceEntry] = Field(max_length=10)
    projects: list[ProjectEntry] = Field(max_length=10)
    education: list[EducationEntry] = Field(max_length=5)
    certifications: list[str] = Field(max_length=10)
    publications: list[str] = Field(max_length=5)
    keywords_emphasized: list[str] = Field(max_length=25)
    change_notes: list[str] = Field(max_length=10)  # concise factual notes, no reasoning

    def all_skill_items(self) -> list[str]:
        return [item for group in self.skills for item in group.items]

    def free_text(self) -> list[str]:
        """Every piece of prose that could carry a claim (for metric/technology checks)."""
        texts = [self.summary, *self.publications, *self.certifications]
        texts += [b for e in self.experience for b in e.bullets]
        texts += [b for p in self.projects for b in p.bullets]
        return [t for t in texts if t]


_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": _STR}


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


TAILORED_RESUME_SCHEMA: dict[str, Any] = _obj(
    {
        "section_order": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "summary",
                    "skills",
                    "experience",
                    "projects",
                    "education",
                    "publications",
                ],
            },
        },
        "summary": _STR,
        "skills": {"type": "array", "items": _obj({"category": _STR, "items": _STR_LIST})},
        "experience": {
            "type": "array",
            "items": _obj(
                {
                    "company": _STR,
                    "title": _STR,
                    "start_date": _STR,
                    "end_date": _STR,
                    "location": _STR,
                    "bullets": _STR_LIST,
                }
            ),
        },
        "projects": {
            "type": "array",
            "items": _obj({"name": _STR, "technologies": _STR_LIST, "bullets": _STR_LIST}),
        },
        "education": {
            "type": "array",
            "items": _obj(
                {
                    "institution": _STR,
                    "degree": _STR,
                    "field_of_study": _STR,
                    "graduation_year": _STR,
                    "grade": _STR,
                }
            ),
        },
        "certifications": _STR_LIST,
        "publications": _STR_LIST,
        "keywords_emphasized": _STR_LIST,
        "change_notes": _STR_LIST,
    }
)


class TailoringOutputError(ValueError):
    """Claude's reply was not a valid structured resume."""


def parse_tailored(payload: str | dict[str, Any]) -> TailoredResume:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise TailoringOutputError(f"Reply is not valid JSON: {exc}") from exc
    try:
        return TailoredResume.model_validate(payload)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}" for e in exc.errors()
        )
        raise TailoringOutputError(f"Invalid resume structure: {problems}") from exc
