"""Read-only view of lead -> trusted ATS resolution: lead and board states, the negative
cache and the configured budgets. Nothing here resolves, approves or crawls anything."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.api.deps import sources_config_dep
from database.base import utcnow
from database.enums import BoardStatus, LeadStatus
from database.models import EmployerBoard, JobLead
from database.session import get_db
from integrations.job_sources.config import JobSourcesConfig

router = APIRouter(prefix="/leads", tags=["leads"])


class BoardOut(BaseModel):
    company: str
    platform: str
    board_id: str
    status: str
    active: bool
    configured: bool
    rule: str | None
    leads: int


class LeadSummary(BaseModel):
    enabled: bool
    auto_activate_verified: bool
    max_requests_per_run: int
    max_requests_per_company: int
    max_companies_per_run: int
    negative_cache_days: int
    leads_total: int
    companies_total: int
    leads_by_status: dict[str, int]
    boards_by_status: dict[str, int]
    negative_cache_active: int  # companies with no board, not re-checked until expiry
    boards_needing_review: list[BoardOut]  # CANDIDATE, or VERIFIED but not yet approved
    active_boards: list[BoardOut]


def _board_out(board: EmployerBoard, leads: int) -> BoardOut:
    return BoardOut(
        company=board.company_name,
        platform=board.platform or "",
        board_id=board.board_id or "",
        status=board.status.value,
        active=board.active,
        configured=board.configured,
        rule=(board.evidence or {}).get("rule"),
        leads=leads,
    )


@router.get("/summary", response_model=LeadSummary)
def lead_summary(
    db: Annotated[Session, Depends(get_db)],
    config: Annotated[JobSourcesConfig, Depends(sources_config_dep)],
) -> LeadSummary:
    rules = config.lead_resolution
    leads_by_status = {
        status.value: count
        for status, count in db.execute(
            select(JobLead.status, func.count()).group_by(JobLead.status)
        ).all()
    }
    boards = db.scalars(select(EmployerBoard).order_by(EmployerBoard.company_name)).all()
    lead_counts = dict(
        db.execute(
            select(JobLead.employer_board_id, func.count())
            .where(JobLead.employer_board_id.is_not(None))
            .group_by(JobLead.employer_board_id)
        ).all()
    )
    now = utcnow()
    real = [b for b in boards if b.platform]
    return LeadSummary(
        enabled=rules.enabled,
        auto_activate_verified=rules.auto_activate_verified,
        max_requests_per_run=rules.max_requests_per_run,
        max_requests_per_company=rules.max_requests_per_company,
        max_companies_per_run=rules.max_companies_per_run,
        negative_cache_days=rules.negative_cache_days,
        leads_total=sum(leads_by_status.values()),
        companies_total=db.scalar(select(func.count(func.distinct(JobLead.normalized_company))))
        or 0,
        leads_by_status={s.value: leads_by_status.get(s.value, 0) for s in LeadStatus},
        boards_by_status={s.value: sum(1 for b in boards if b.status is s) for s in BoardStatus},
        negative_cache_active=sum(
            1
            for b in boards
            if b.status is BoardStatus.NOT_FOUND and b.next_check_at and b.next_check_at > now
        ),
        boards_needing_review=[
            _board_out(b, lead_counts.get(b.id, 0))
            for b in real
            if b.status is BoardStatus.CANDIDATE
            or (b.status is BoardStatus.VERIFIED and not b.active and not b.configured)
        ],
        active_boards=[_board_out(b, lead_counts.get(b.id, 0)) for b in real if b.active],
    )
