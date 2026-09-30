"""Reporting tools: statistics and a local export (CSV/JSON; XLSX arrives in Phase 7)."""

import csv
import json
from typing import Literal

from sqlalchemy import func, select

from agent.tools.base import NoInput, Tool, ToolContext, ToolInput, ToolOutput
from database.base import utcnow
from database.enums import ToolPermission
from database.models import Application, Job


class StatisticsOutput(ToolOutput):
    jobs_total: int
    jobs_by_status: dict[str, int]
    applications_total: int
    applications_by_status: dict[str, int]
    submitted_applications: int
    blocked_needing_user: int


def get_application_statistics(ctx: ToolContext, _: NoInput) -> StatisticsOutput:
    by_job = dict(
        ctx.db.execute(select(Job.pipeline_state, func.count()).group_by(Job.pipeline_state)).all()
    )
    by_app = dict(
        ctx.db.execute(select(Application.status, func.count()).group_by(Application.status)).all()
    )
    jobs_by_status = {k.value: v for k, v in by_job.items()}
    return StatisticsOutput(
        jobs_total=sum(by_job.values()),
        jobs_by_status=jobs_by_status,
        applications_total=sum(by_app.values()),
        applications_by_status={k.value: v for k, v in by_app.items()},
        # Only the browser workflow (not enabled yet) can ever set SUBMITTED.
        submitted_applications=jobs_by_status.get("SUBMITTED", 0),
        blocked_needing_user=jobs_by_status.get("BLOCKED_NEEDS_USER", 0),
    )


class ExportInput(ToolInput):
    format: Literal["csv", "json"] = "csv"


class ExportOutput(ToolOutput):
    path: str
    rows: int
    format: str


def export_application_data(ctx: ToolContext, p: ExportInput) -> ExportOutput:
    rows = []
    for job in ctx.db.scalars(select(Job).order_by(Job.discovered_at)):
        app = ctx.db.scalar(select(Application).where(Application.job_id == job.id))
        rows.append(
            {
                "company": job.company.name if job.company else "",
                "title": job.title,
                "location": job.location or "",
                "posted_at": job.posted_at.isoformat() if job.posted_at else "",
                "job_status": job.pipeline_state.value,
                "application_status": app.status.value if app else "",
                "user_action_required": (app.user_action_required or "") if app else "",
                "url": job.url,
            }
        )
    ctx.settings.export_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.settings.export_dir / f"applications-{utcnow():%Y%m%d-%H%M%S}.{p.format}"
    if p.format == "json":
        path.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    else:
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["company"])
            writer.writeheader()
            writer.writerows(rows)
    return ExportOutput(path=str(path), rows=len(rows), format=p.format)


TOOLS = [
    Tool(
        name="get_application_statistics",
        description="Counts of jobs by pipeline status and applications by status.",
        permission=ToolPermission.READ_ONLY,
        input_model=NoInput,
        output_model=StatisticsOutput,
        handler=get_application_statistics,
    ),
    Tool(
        name="export_application_data",
        description="Write a CSV or JSON export of jobs and applications to data/exports/ "
        "(a local file; nothing leaves the machine).",
        permission=ToolPermission.WRITE,
        input_model=ExportInput,
        output_model=ExportOutput,
        handler=export_application_data,
    ),
]
