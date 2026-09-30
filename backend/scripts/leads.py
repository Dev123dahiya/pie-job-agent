"""Review aggregator leads and the employer ATS boards they resolve to (Phase 5.7).

python -m backend.scripts.leads list [--status NEW] [--limit 50]
python -m backend.scripts.leads boards [--status CANDIDATE]
python -m backend.scripts.leads boards --approve greenhouse:acmetech   (or a board id prefix)
python -m backend.scripts.leads boards --reject greenhouse:acmetech

A lead is never a job and never makes anything fresh. Approving a board only allows PIE to
read that employer's public ATS board; its postings then go through the normal pipeline.
Nothing here applies to jobs, opens application pages or contacts an employer.
"""

import argparse
import json
import sys

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.core.logging import configure_logging
from database.base import utcnow
from database.enums import BoardStatus, LeadStatus
from database.models import EmployerBoard, JobLead
from database.session import get_session_factory


def _board(db: Session, ref: str) -> EmployerBoard | None:
    if ":" in ref:
        platform, _, board_id = ref.partition(":")
        return db.scalar(
            select(EmployerBoard).where(
                EmployerBoard.platform == platform.lower(),
                func.lower(EmployerBoard.board_id) == board_id.lower(),
            )
        )
    matches = [b for b in db.scalars(select(EmployerBoard)).all() if str(b.id).startswith(ref)]
    return matches[0] if len(matches) == 1 and len(ref) >= 6 else None


def cmd_list(args: argparse.Namespace) -> int:
    with get_session_factory()() as db:
        stmt = select(JobLead).order_by(JobLead.last_seen_at.desc()).limit(args.limit)
        if args.status:
            stmt = stmt.where(JobLead.status == LeadStatus(args.status.upper()))
        leads = db.scalars(stmt).all()
        if not leads:
            print("No leads.")
            return 0
        for lead in leads:
            board = lead.board
            print(
                f"{lead.status.value:<15} {lead.source:<9} {lead.company_name or '?'} | "
                f"{lead.title} | {lead.location or '?'}"
            )
            print(
                f"    board: {board.label if board else '-'}"
                f" ({board.status.value if board else '-'}"
                f"{', active' if board and board.active else ''}"
                f"{', configured' if board and board.configured else ''})"
                f"  matched job: {lead.matched_job_id or '-'}"
            )
            print(
                f"    lead timestamp (untrusted, never used for freshness): "
                f"{lead.aggregator_timestamp or '-'}  "
                f"first seen: {lead.first_seen_at:%Y-%m-%d %H:%M}"
            )
            if lead.evidence:
                print(f"    evidence: {json.dumps(lead.evidence, default=str)}")
            if lead.source == "himalayas":
                print(f"    {lead.source_url}  (job data from Himalayas, https://himalayas.app)")
    return 0


def cmd_boards(args: argparse.Namespace) -> int:
    with get_session_factory()() as db:
        if args.approve or args.reject:
            board = _board(db, args.approve or args.reject)
            if board is None or board.platform is None:
                print(
                    "No such board (use platform:board_id or a board id prefix).", file=sys.stderr
                )
                return 1
            now = utcnow()
            if args.approve:
                board.status = BoardStatus.VERIFIED
                board.active = not board.configured  # configured boards are crawled anyway
                board.approved_at = now
                board.evidence = {
                    **(board.evidence or {}),
                    "approved_by": "user",
                    "at": now.isoformat(),
                }
                print(f"Approved {board.label} for {board.company_name}: active={board.active}")
            else:
                board.status = BoardStatus.REJECTED
                board.active = False
                board.evidence = {
                    **(board.evidence or {}),
                    "rejected_by": "user",
                    "at": now.isoformat(),
                }
                for lead in board.leads:
                    if lead.status in (LeadStatus.BOARD_CANDIDATE, LeadStatus.VERIFIED):
                        lead.status = LeadStatus.NO_BOARD
                print(f"Rejected {board.label} for {board.company_name}")
            db.commit()
            return 0
        stmt = select(EmployerBoard).order_by(EmployerBoard.status, EmployerBoard.company_name)
        if args.status:
            stmt = stmt.where(EmployerBoard.status == BoardStatus(args.status.upper()))
        boards = db.scalars(stmt).all()
        if not boards:
            print("No boards.")
            return 0
        for b in boards:
            leads = len(b.leads)
            print(
                f"{str(b.id)[:8]}  {b.status.value:<9} {'ACTIVE' if b.active else 'inactive':<8} "
                f"{'configured ' if b.configured else ''}{b.label:<40} {b.company_name} "
                f"(leads: {leads})"
            )
            if b.evidence:
                print(f"          evidence: {json.dumps(b.evidence, default=str)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging(get_settings())
    parser = argparse.ArgumentParser(prog="python -m backend.scripts.leads")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="list aggregator leads")
    ls.add_argument(
        "--status", choices=[s.value for s in LeadStatus] + [s.value.lower() for s in LeadStatus]
    )
    ls.add_argument("--limit", type=int, default=50)
    ls.set_defaults(func=cmd_list)
    bd = sub.add_parser("boards", help="list, approve or reject employer boards")
    bd.add_argument(
        "--status", choices=[s.value for s in BoardStatus] + [s.value.lower() for s in BoardStatus]
    )
    group = bd.add_mutually_exclusive_group()
    group.add_argument("--approve", metavar="BOARD", help="platform:board_id or id prefix")
    group.add_argument("--reject", metavar="BOARD", help="platform:board_id or id prefix")
    bd.set_defaults(func=cmd_boards)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
