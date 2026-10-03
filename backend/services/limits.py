"""Daily application limits (REQUIREMENTS 10), enforced in the backend.

A total limit plus one per platform, read from config/automation.yaml. What counts: every live
submission attempt made today (the user's calendar day), whatever its outcome, except one the
user marked as never sent. A pending or ambiguous attempt may have reached the employer, so it
uses up the limit too. Dry runs never submit, so they never count.

Checked twice: before a live session opens (nothing is started when the limit is used up) and
again right before the final submit click (two sessions prepared at once can't both slip
through).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.services.platforms import PLATFORMS, Platform, platform_of
from database.enums import SubmissionOutcome
from database.models import Application, Job, SubmissionAttempt


class LimitsConfigError(Exception):
    """config/automation.yaml is unreadable or invalid. Live applying stops until it is fixed."""


class DailyLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(default=20, ge=0, le=200)
    naukri: int = Field(default=15, ge=0, le=200)
    linkedin: int = Field(default=5, ge=0, le=200)
    company_site: int = Field(default=10, ge=0, le=200)

    @model_validator(mode="after")
    def _linkedin_below_naukri(self) -> "DailyLimits":
        if self.linkedin > 0 and self.linkedin >= self.naukri:
            raise ValueError("the LinkedIn limit must be lower than the Naukri limit")
        return self

    def for_platform(self, platform: Platform) -> int:
        return int(getattr(self, platform))


class Models(BaseModel):
    """Claude model per task (REQUIREMENTS 6): a cheaper one for scoring many jobs, the
    strongest for tailoring resumes and answering application questions."""

    model_config = ConfigDict(extra="forbid")

    scoring: str = Field(default="claude-sonnet-5-5", pattern=r"^claude-[a-z0-9.\-]+$")
    tailoring: str = Field(default="claude-opus-5-5", pattern=r"^claude-[a-z0-9.\-]+$")
    answers: str = Field(default="claude-opus-5-5", pattern=r"^claude-[a-z0-9.\-]+$")


class AutomationConfig(BaseModel):
    model_config = ConfigDict(extra="allow")  # later steps add modes etc.

    daily_limits: DailyLimits = Field(default_factory=lambda: DailyLimits())
    models: Models = Field(default_factory=lambda: Models())


def load_automation(path: Path) -> AutomationConfig:
    """A missing file means the agreed defaults; a broken file is an error (fail closed)."""
    if not path.exists():
        return AutomationConfig()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return AutomationConfig.model_validate(data)
    except (yaml.YAMLError, ValidationError, OSError) as exc:
        raise LimitsConfigError(f"{path.name} is invalid: {exc}") from exc


def start_of_today(tz: str, now: datetime | None = None) -> datetime:
    zone = ZoneInfo(tz)
    local_now = (now or datetime.now(UTC)).astimezone(zone)
    return datetime.combine(local_now.date(), time.min, tzinfo=zone)


def used_today(db: Session, tz: str, now: datetime | None = None) -> dict[Platform, int]:
    since = start_of_today(tz, now)
    rows = db.execute(
        select(Job.source)
        .select_from(SubmissionAttempt)
        .join(Application, Application.id == SubmissionAttempt.application_id)
        .join(Job, Job.id == Application.job_id)
        .where(
            SubmissionAttempt.attempted_at >= since,
            SubmissionAttempt.outcome != SubmissionOutcome.USER_NOT_SUBMITTED,
        )
    ).all()
    counts: dict[Platform, int] = dict.fromkeys(PLATFORMS, 0)
    for (source,) in rows:
        counts[platform_of(Job(source=source))] += 1
    return counts


@dataclass(frozen=True)
class LimitStatus:
    platform: str
    used: int
    limit: int

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)


def usage(db: Session, path: Path, tz: str, now: datetime | None = None) -> list[LimitStatus]:
    """Today's use against every limit: the total first, then each platform."""
    limits = load_automation(path).daily_limits
    used = used_today(db, tz, now)
    return [LimitStatus("total", sum(used.values()), limits.total)] + [
        LimitStatus(p, used[p], limits.for_platform(p)) for p in PLATFORMS
    ]


def limit_block(
    db: Session, path: Path, tz: str, job: Job, now: datetime | None = None
) -> str | None:
    """Why one more live application for this job is not allowed today, or None."""
    try:
        statuses = {s.platform: s for s in usage(db, path, tz, now)}
    except LimitsConfigError as exc:
        return f"Daily limits can't be read, so nothing is submitted: {exc}"
    platform = platform_of(job)
    for status in (statuses["total"], statuses[platform]):
        if status.remaining == 0:
            return (
                f"Daily {status.platform} limit reached ({status.used} of {status.limit} today). "
                "Nothing was submitted; it resets at midnight."
            )
    return None


def model_for(path: Path, task: str) -> str:
    """The configured Claude model for ``task`` (scoring | tailoring | answers)."""
    models = load_automation(path).models
    return str(getattr(models, task))
