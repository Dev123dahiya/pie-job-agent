"""Daily auto-apply: after the daily discovery + analysis, apply in AUTO mode on every platform
where the user unlocked it (REQUIREMENTS 10).

Off unless DAILY_AUTO_APPLY=true. That setting is the user's standing confirmation: it stands
in for the phrase typed on the Runs page. Every other safety rule is the normal pipeline's
(live switches, visible browser, auto unlock per platform, daily limits counted before
opening and before submitting, duplicate checks, stop on site warnings, pause on any
question without a verified answer). Company sites need a tailored resume (Claude), so this
step only drafts jobs that need no tailoring: Naukri and LinkedIn.
"""

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agent.tools import ToolContext, ToolRegistry
from backend.core.config import Settings, live_execution_block
from backend.core.profile import UserProfile
from backend.services.auto_mode import auto_status
from backend.services.pipeline import (
    PipelineDeps,
    PipelineError,
    PipelineRequest,
    PipelineService,
    live_confirmation,
    start_pipeline,
)
from backend.services.platforms import Platform, platform_of
from database.enums import JobPipelineState, ToolPermission
from database.models import Job

logger = logging.getLogger(__name__)

DAILY_AUTO_PLATFORMS: tuple[Platform, ...] = ("linkedin", "naukri")  # no tailored resume needed


def _no_plan() -> Any:
    raise RuntimeError("daily auto-apply runs only the apply step")


def _draft(db: Session, settings: Settings, profile: UserProfile, registry: ToolRegistry,
           platform: str) -> int:  # fmt: skip
    """Application drafts for the platform's shortlisted jobs (no Claude). Returns how many
    jobs are ready to apply."""
    allowed = frozenset({ToolPermission.READ_ONLY, ToolPermission.WRITE})
    ctx = ToolContext(db=db, settings=settings, profile=profile, actor="system", allowed=allowed)
    for job in db.scalars(
        select(Job).where(Job.pipeline_state == JobPipelineState.SHORTLISTED)
    ).all():
        if platform_of(job) == platform:
            registry.execute("create_application_draft", {"job_id": str(job.id)}, ctx)
    db.commit()
    ready = db.scalars(
        select(Job).where(Job.pipeline_state == JobPipelineState.READY_TO_APPLY)
    ).all()
    return sum(1 for j in ready if platform_of(j) == platform)


def run_daily_auto(
    factory: sessionmaker[Session],
    settings: Settings,
    profile: UserProfile,
    registry: ToolRegistry,
    platform_browser: Callable[[str], Any],
) -> dict[str, Any]:
    """One auto-apply run per unlocked platform. Returns {platform: outcome}."""
    out: dict[str, Any] = {}
    if not settings.daily_auto_apply:
        return {"skipped": "DAILY_AUTO_APPLY is off"}
    block = live_execution_block(settings)
    if block is not None:
        return {"skipped": block[1]}
    for platform in DAILY_AUTO_PLATFORMS:
        with factory() as db:
            status = auto_status(db, platform)
            if not status["unlocked"]:
                out[platform] = {"skipped": "auto mode not unlocked", "missing": status["missing"]}
                continue
            ready = _draft(db, settings, profile, registry, platform)
            expected = live_confirmation(db, settings, platform, "auto")
            if ready == 0 or expected["remaining"] == 0:
                out[platform] = {"skipped": "nothing ready" if ready == 0 else "daily limit used"}
                continue
            req = PipelineRequest(steps=("apply",), platforms=(platform,), mode="auto",
                                  apply_limit=expected["remaining"])  # fmt: skip
            try:
                run_id = start_pipeline(db, settings, req, expected["phrase"]).id
            except PipelineError as exc:
                out[platform] = {"skipped": str(exc)}
                continue
        deps = PipelineDeps(
            plan=_no_plan, settings=settings, profile=profile, registry=registry,
            browser=None, platform_browser=platform_browser,
        )  # fmt: skip
        report = PipelineService(factory).execute(run_id, req, deps)
        out[platform] = {"run_id": str(run_id), "counts": report["counts"],
                         "failures": len(report["failures"])}  # fmt: skip
        logger.info("Daily auto-apply on %s: %s", platform, report["counts"])
    return out
