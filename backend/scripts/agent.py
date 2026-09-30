"""Run and inspect the autonomous agent from the command line.

    python -m backend.scripts.agent run "Summarize my pipeline" --mode read_only
    python -m backend.scripts.agent show <run_id>
    python -m backend.scripts.agent resume <run_id> --message "Yes, authorized in India"
    python -m backend.scripts.agent cancel <run_id>

External actions (submitting, emailing, browsing) are disabled in Phase 3.
"""

import argparse
import sys
import uuid

from sqlalchemy import select

from agent.analysis.analyzers import (
    AnalyzerUnavailableError,
    resolve_analyzer,
    resolve_structured_claude,
)
from backend.core.config import get_settings
from backend.core.logging import configure_logging
from backend.core.profile import UserProfile
from backend.services.agent_runner import AgentRunner, resume_agent_run, start_agent_run
from backend.services.analysis import AnalysisContext
from backend.services.runs import RunConflictError, request_cancel
from backend.services.shortlist import load_shortlist_rules
from database.models import AgentAction, AgentRun
from database.session import get_session_factory
from integrations.job_sources.config import load_job_sources_config


def _runner() -> AgentRunner:
    settings = get_settings()
    config = load_job_sources_config(settings.job_sources_path)
    rules = load_shortlist_rules(settings.shortlist_rules_path)

    def analysis_context(profile: UserProfile) -> AnalysisContext:
        analyzer = resolve_analyzer(
            settings, config.search.location_patterns, config.search.term_aliases
        )
        return AnalysisContext(
            analyzer=analyzer, profile=profile, rules=rules, replace_outdated=True
        )

    return AgentRunner(
        get_session_factory(),
        resolve_structured_claude(settings),
        settings=settings,
        analysis_context_builder=analysis_context,
    )


def _show(run_id: uuid.UUID) -> None:
    with get_session_factory()() as db:
        run = db.get(AgentRun, run_id)
        if run is None:
            print("Run not found", file=sys.stderr)
            return
        print(f"{run.status.value}  steps {run.iterations}/{run.max_iterations}  mode {run.mode}")
        print(f"task: {run.task}")
        for a in db.scalars(
            select(AgentAction).where(AgentAction.run_id == run_id).order_by(AgentAction.sequence)
        ):
            detail = a.reasoning or a.error or ""
            tool = f" [{a.tool}]" if a.tool else ""
            print(f"  {a.sequence:3} {a.tool_name:20} {a.status.value:8}{tool} {detail[:140]}")
        if run.pending_user_input:
            print(f"\nNEEDS YOUR INPUT: {run.pending_user_input}")
        if run.summary:
            print(f"\nresult: {run.summary}")


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252; results contain characters like the rupee sign.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    configure_logging(get_settings())
    parser = argparse.ArgumentParser(prog="python -m backend.scripts.agent")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run")
    r.add_argument("task")
    r.add_argument("--mode", choices=["read_only", "internal_writes"], default="internal_writes")
    r.add_argument("--max-iterations", type=int, default=12)
    s = sub.add_parser("show")
    s.add_argument("run_id", type=uuid.UUID)
    rs = sub.add_parser("resume")
    rs.add_argument("run_id", type=uuid.UUID)
    rs.add_argument("--message")
    rs.add_argument("--extra-iterations", type=int, default=0)
    c = sub.add_parser("cancel")
    c.add_argument("run_id", type=uuid.UUID)
    args = parser.parse_args(argv)

    if args.command == "show":
        _show(args.run_id)
        return 0
    if args.command == "cancel":
        with get_session_factory()() as db:
            run = request_cancel(db, args.run_id)
            print(run.status.value if run else "Run not found")
        return 0
    try:
        runner = _runner()
    except AnalyzerUnavailableError as exc:
        print(exc, file=sys.stderr)
        return 1
    with get_session_factory()() as db:
        try:
            if args.command == "run":
                run = start_agent_run(
                    db,
                    args.task,
                    mode=args.mode,
                    max_iterations=args.max_iterations,
                    dev_mode=get_settings().development_mode,
                    backend=runner.claude.name,
                )
            else:
                run = resume_agent_run(
                    db, args.run_id, message=args.message, extra_iterations=args.extra_iterations
                )
        except (RunConflictError, ValueError, LookupError) as exc:
            print(exc, file=sys.stderr)
            return 1
        run_id = run.id
    print(f"run {run_id} ({runner.claude.name})")
    runner.execute(run_id)
    _show(run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
