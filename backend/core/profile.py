"""User profile, loaded from YAML (never hardcoded).

The file has two strictly separate sections:

* ``verified_profile`` - FACTS about the user (education, experience, skills...). The only
  source the agent may use when describing the user or judging their fit.
* ``preferences`` - what the user WANTS (roles, locations, salary...). Never treated as facts.

Reads config/user_profile.yaml; falls back to config/user_profile.example.yaml so a fresh
checkout still boots. The Phase 1 flat layout (facts at the top level) is still accepted.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, EmailStr, Field, model_validator

from backend.core.config import get_settings


class Education(BaseModel):
    institution: str
    degree: str
    field_of_study: str | None = None
    graduation_year: int | None = None
    grade: str | None = None


class WorkExperience(BaseModel):
    company: str
    title: str
    start_date: str
    end_date: str | None = None
    location: str | None = None
    highlights: list[str] = Field(default_factory=list)


class Project(BaseModel):
    name: str
    description: str
    technologies: list[str] = Field(default_factory=list)
    url: str | None = None


class VerifiedProfile(BaseModel):
    """Facts only. Anything absent here is unknown to the agent."""

    name: str
    # Optional identity parts. When absent, first/last name are derived from `name` only if it
    # is exactly two words; otherwise they are UNCONFIRMED and asked of the user.
    first_name: str | None = None
    last_name: str | None = None
    email: EmailStr
    phone: str | None = None
    location: str | None = None
    links: dict[str, str] = Field(default_factory=dict)
    total_experience_years: float = 0
    education: list[Education] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    work_experience: list[WorkExperience] = Field(default_factory=list)
    # Answers only the user can give (work authorization, notice period...). Anything
    # absent here must be surfaced as requires_user_input rather than guessed.
    verified_answers: dict[str, str] = Field(default_factory=dict)
    # Skills that appear on the resume but that the user has NOT confirmed. Kept for the
    # record, never treated as verified: excluded from analysis input and from matching.
    unconfirmed_skills: list[str] = Field(default_factory=list)

    def _unconfirmed(self) -> set[str]:
        return {s.strip().lower() for s in self.unconfirmed_skills}

    def confirmed_skills(self) -> list[str]:
        """The skills list minus anything marked unconfirmed."""
        blocked = self._unconfirmed()
        return [s for s in self.skills if s.strip().lower() not in blocked]

    def all_skills(self) -> list[str]:
        """Skills the user verifiably has: confirmed skills plus project technologies."""
        blocked = self._unconfirmed()
        seen: dict[str, str] = {}
        for skill in [*self.skills, *(t for p in self.projects for t in p.technologies)]:
            key = skill.strip().lower()
            if key not in blocked:
                seen.setdefault(key, skill.strip())
        return list(seen.values())


class JobPreferences(BaseModel):
    """What the user wants. Preferences, not facts."""

    preferred_roles: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)
    # Missing salary on a posting is "unknown", not a rejection.
    minimum_salary: float | None = None
    salary_currency: str = "INR"
    employment_type: list[str] = Field(default_factory=lambda: ["full-time"])
    remote_preference: str = "any"  # any | remote | hybrid | onsite
    experience_levels: list[str] = Field(default_factory=list)
    excluded_roles: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)
    # Jobs posted within this window are "fresh"; older are "stale"; no date is "unknown".
    maximum_application_age_hours: int = 48
    daily_application_limit: int = 20  # unused; limits are in config/automation.yaml (B7)


_VERIFIED_KEYS = set(VerifiedProfile.model_fields)


class UserProfile(BaseModel):
    verified_profile: VerifiedProfile
    preferences: JobPreferences = Field(default_factory=JobPreferences)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_flat_layout(cls, data: Any) -> Any:
        if isinstance(data, dict) and "verified_profile" not in data:
            verified = {k: v for k, v in data.items() if k in _VERIFIED_KEYS}
            rest = {k: v for k, v in data.items() if k not in _VERIFIED_KEYS}
            return {**rest, "verified_profile": verified}
        return data

    # Convenience accessors for the most-used facts.
    @property
    def name(self) -> str:
        return self.verified_profile.name

    @property
    def email(self) -> str:
        return str(self.verified_profile.email)


def resolve_profile_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    primary = get_settings().user_profile_path
    return primary if primary.exists() else primary.with_name("user_profile.example.yaml")


def load_user_profile(path: Path | None = None) -> UserProfile:
    with resolve_profile_path(path).open(encoding="utf-8") as fh:
        return UserProfile.model_validate(yaml.safe_load(fh))
