"""Settings page (REQUIREMENTS 19.2.12): edit search settings, limits, the minimum score and
the standard answers. Every save: validate, show what changes, back up the old file, save.

Edits go through backend/core/yaml_edit.py, which changes only the edited keys (the files'
comments stay) and checks the result parses to exactly the intended data. Each whole file is
then validated with the same model the app loads it with; any error and nothing is written.
Keywords and locations are written to both the search config and the profile so they never
drift apart (B10). Backups: data/backups/config/<file>.<timestamp>.yaml.
"""

import difflib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from backend.core.config import REPO_ROOT, Settings
from backend.core.profile import UserProfile, resolve_profile_path
from backend.core.yaml_edit import YamlEditError, set_value
from backend.services.limits import AutomationConfig, DailyLimits
from backend.services.readiness import STANDARD_QUESTIONS
from backend.services.shortlist import ShortlistRules
from integrations.job_sources.config import JobSourcesConfig

BACKUP_DIR = REPO_ROOT / "data" / "backups" / "config"
ANSWER_KEYS = tuple(STANDARD_QUESTIONS)  # total_experience comes from the profile's years


class SettingsError(ValueError):
    """Invalid settings; nothing was saved. The message says what to fix."""


def _clean_list(values: list[str], *, max_len: int) -> list[str]:
    out: list[str] = []
    for v in values:
        v = " ".join(v.split())
        if not v:
            continue
        if len(v) > max_len:
            raise ValueError(f"'{v[:30]}…' is longer than {max_len} characters")
        if v.lower() not in {o.lower() for o in out}:
            out.append(v)
    return out


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total: int = Field(ge=0, le=200)
    naukri: int = Field(ge=0, le=200)
    linkedin: int = Field(ge=0, le=200)
    company_site: int = Field(ge=0, le=200)


class SettingsChange(BaseModel):
    """Only the fields present are changed."""

    model_config = ConfigDict(extra="forbid")

    keywords: list[str] | None = Field(None, max_length=30)
    locations: list[str] | None = Field(None, max_length=20)
    excluded_companies: list[str] | None = Field(None, max_length=200)
    excluded_keywords: list[str] | None = Field(None, max_length=100)
    freshness_hours: int | None = Field(None, ge=1, le=720)
    limits: Limits | None = None
    min_match_score: int | None = Field(None, ge=0, le=100)
    answers: dict[str, str] | None = None

    @field_validator("keywords", "locations")
    @classmethod
    def _required_list(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        cleaned = _clean_list(v, max_len=80)
        if not cleaned:
            raise ValueError("needs at least one entry")
        return cleaned

    @field_validator("excluded_companies", "excluded_keywords")
    @classmethod
    def _optional_list(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _clean_list(v, max_len=120)

    @field_validator("answers")
    @classmethod
    def _answers(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return v
        out = {}
        for key, text in v.items():
            if key not in ANSWER_KEYS:
                raise ValueError(f"'{key}' is not a standard answer ({', '.join(ANSWER_KEYS)})")
            text = " ".join(text.split())
            if not text:
                raise ValueError(f"the answer for '{key}' is empty")
            if len(text) > 200:
                raise ValueError(f"the answer for '{key}' is longer than 200 characters")
            out[key] = text
        return out


@dataclass
class FileChange:
    name: str
    path: Path
    before: str
    after: str

    def diff(self) -> str:
        return "".join(
            difflib.unified_diff(
                self.before.splitlines(keepends=True),
                self.after.splitlines(keepends=True),
                fromfile=f"{self.name} (now)",
                tofile=f"{self.name} (after saving)",
                n=1,
            )
        )


def _paths(settings: Settings) -> dict[str, Path]:
    return {
        "job_sources.yaml": settings.job_sources_path,
        "user_profile.yaml": resolve_profile_path(settings.user_profile_path),
        "automation.yaml": settings.automation_path,
        "shortlist_rules.yaml": settings.shortlist_rules_path,
    }


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def current(settings: Settings) -> dict[str, Any]:
    """What the Settings page shows. Answers are shown only to the dashboard on this computer."""
    p = _paths(settings)
    sources = yaml.safe_load(_read(p["job_sources.yaml"])) or {}
    profile = yaml.safe_load(_read(p["user_profile.yaml"])) or {}
    prefs = profile.get("preferences") or {}
    answers = (profile.get("verified_profile") or {}).get("verified_answers") or {}
    automation = AutomationConfig.model_validate(yaml.safe_load(_read(p["automation.yaml"])) or {})
    rules = ShortlistRules.model_validate(yaml.safe_load(_read(p["shortlist_rules.yaml"])) or {})
    search = sources.get("search") or {}
    return {
        "keywords": list(search.get("terms") or []),
        "locations": list(search.get("locations") or []),
        "excluded_companies": list(prefs.get("excluded_companies") or []),
        "excluded_keywords": list(prefs.get("excluded_roles") or []),
        "freshness_hours": prefs.get("maximum_application_age_hours", 48),
        "limits": automation.daily_limits.model_dump(),
        "min_match_score": rules.min_match_score,
        "answers": {k: str(answers[k]) for k in ANSWER_KEYS if answers.get(k) not in (None, "")},
        "answer_questions": dict(STANDARD_QUESTIONS),
        "read_only": {
            "models": {
                "note": "Model per task (scoring, tailoring, answers) arrives with step E1, "
                "right before the first big analysis run.",
                "current": settings.claude_model,
            },
            "experience_range": "Arrives with the Naukri search (Phase C).",
            "modes": "Choose the mode per run on the Runs page. Auto mode arrives in Phase E.",
        },
    }


def plan_changes(settings: Settings, change: SettingsChange) -> list[FileChange]:
    """The new text of every file the change touches, validated. Raises SettingsError."""
    p = _paths(settings)
    texts = {name: _read(path) for name, path in p.items()}
    edits: list[tuple[str, list[str], Any]] = []
    profile, sources = "user_profile.yaml", "job_sources.yaml"
    if change.keywords is not None:
        edits.append((sources, ["search", "terms"], change.keywords))
        edits.append((profile, ["preferences", "preferred_roles"], change.keywords))
    if change.locations is not None:
        edits.append((sources, ["search", "locations"], change.locations))
        edits.append((profile, ["preferences", "preferred_locations"], change.locations))
    if change.excluded_companies is not None:
        edits.append((profile, ["preferences", "excluded_companies"], change.excluded_companies))
    if change.excluded_keywords is not None:
        edits.append((profile, ["preferences", "excluded_roles"], change.excluded_keywords))
    if change.freshness_hours is not None:
        hours = ["preferences", "maximum_application_age_hours"]
        edits.append((profile, hours, change.freshness_hours))
    if change.limits is not None:
        try:
            DailyLimits.model_validate(change.limits.model_dump())
        except ValidationError as exc:
            raise SettingsError(_first_error(exc)) from exc
        edits.append(("automation.yaml", ["daily_limits"], change.limits.model_dump()))
    if change.min_match_score is not None:
        edits.append(("shortlist_rules.yaml", ["min_match_score"], change.min_match_score))
    for key, text in (change.answers or {}).items():
        edits.append(("user_profile.yaml", ["verified_profile", "verified_answers", key], text))
    if not edits:
        raise SettingsError("Nothing to change")

    new = dict(texts)
    for name, path, value in edits:
        try:
            new[name] = set_value(new[name], path, value)
        except YamlEditError as exc:
            raise SettingsError(f"{name}: {exc}") from exc
    _validate(new)
    return [
        FileChange(name=name, path=p[name], before=texts[name], after=new[name])
        for name in p
        if new[name] != texts[name]
    ]


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    where = ".".join(str(x) for x in err.get("loc", ()))
    return f"{where}: {err.get('msg')}" if where else str(err.get("msg"))


def _validate(texts: dict[str, str]) -> None:
    checks: dict[str, type[BaseModel]] = {
        "job_sources.yaml": JobSourcesConfig,
        "user_profile.yaml": UserProfile,
        "automation.yaml": AutomationConfig,
        "shortlist_rules.yaml": ShortlistRules,
    }
    for name, model in checks.items():
        try:
            model.model_validate(yaml.safe_load(texts[name]) or {})
        except ValidationError as exc:
            raise SettingsError(f"{name}: {_first_error(exc)}") from exc
        except yaml.YAMLError as exc:
            raise SettingsError(f"{name} is not valid YAML: {exc}") from exc


def save_changes(
    settings: Settings, change: SettingsChange, backup_dir: Path = BACKUP_DIR
) -> list[FileChange]:
    """Validate, back up every file that changes, then write it (atomically)."""
    changes = plan_changes(settings, change)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_dir.mkdir(parents=True, exist_ok=True)
    for c in changes:
        if c.before:
            (backup_dir / f"{Path(c.name).stem}.{stamp}.yaml").write_text(
                c.before, encoding="utf-8"
            )
    for c in changes:
        tmp = c.path.with_name(c.path.name + ".tmp")
        tmp.write_text(c.after, encoding="utf-8")
        os.replace(tmp, c.path)
    return changes
