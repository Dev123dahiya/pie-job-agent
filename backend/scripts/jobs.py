"""Run discovery and analysis from the command line.

python -m backend.scripts.jobs discover                 # all enabled sources
python -m backend.scripts.jobs discover --source mock   # offline fixtures
python -m backend.scripts.jobs analyze --limit 10       # analyze + shortlist waiting jobs
python -m backend.scripts.jobs runs                     # recent runs
"""

import argparse
import sys

from sqlalchemy import select

from agent.analysis.analyzers import AnalyzerUnavailableError, resolve_analyzer
from backend.core.config import get_settings
from backend.core.logging import configure_logging
from backend.core.profile import load_user_profile
from backend.services.analysis import (
    AnalysisContext,
    AnalysisService,
    create_analysis_run,
    has_outdated_analysis,
    profile_fingerprint,
    select_jobs_for_batch,
)
from backend.services.discovery import DiscoveryService, create_discovery_run
from backend.services.runs import RunConflictError
from backend.services.shortlist import load_shortlist_rules
from database.base import utcnow
from database.models import AgentRun, Job
from database.session import get_session_factory
from integrations.job_sources.config import load_job_sources_config
from integrations.job_sources.registry import UnknownSourceError, build_source


def cmd_discover(args: argparse.Namespace) -> int:
    settings = get_settings()
    config = load_job_sources_config(settings.job_sources_path)
    profile = load_user_profile()
    names = [args.source] if args.source else [s.name for s in config.enabled_sources()]
    if not names:
        print("No sources enabled in config/job_sources.yaml", file=sys.stderr)
        return 1
    query = config.build_query(
        profile.preferences.preferred_roles,
        profile.preferences.preferred_locations,
        excluded_companies=profile.preferences.excluded_companies,
        max_age_hours=profile.preferences.maximum_application_age_hours,
    )
    factory = get_session_factory()
    service = DiscoveryService(factory)
    exit_code = 0
    for name in names:
        try:
            source = build_source(name, config, settings)
        except (UnknownSourceError, ValueError) as exc:
            print(f"[{name}] {exc}", file=sys.stderr)
            exit_code = 1
            continue
        with factory() as db:
            try:
                run = create_discovery_run(db, source, query, settings.development_mode)
            except RunConflictError as exc:
                print(exc, file=sys.stderr)
                return 1
        print(f"[{name}] run {run.id}: {len(query.terms)} terms x {len(query.locations)} locations")
        s = service.execute(run.id, source, query)
        print(
            f"[{name}] {s.status.value}: searches {s.searches_completed}/{s.searches_total}, "
            f"found {s.jobs_found}, new {s.jobs_new}, duplicates {s.jobs_duplicate}, "
            f"failed {s.jobs_failed}, errors {s.errors}"
        )
        if s.status.value == "FAILED":
            exit_code = 1
    return exit_code


def cmd_analyze(args: argparse.Namespace) -> int:
    settings = get_settings()
    config = load_job_sources_config(settings.job_sources_path)
    rules = load_shortlist_rules(settings.shortlist_rules_path)
    profile = load_user_profile()
    try:
        analyzer = resolve_analyzer(
            settings, config.search.location_patterns, config.search.term_aliases
        )
    except AnalyzerUnavailableError as exc:
        print(exc, file=sys.stderr)
        return 1
    include_stale = args.include_stale or rules.analyze_stale_jobs_in_batch
    factory = get_session_factory()
    with factory() as db:
        job_ids = list(
            db.scalars(
                select_jobs_for_batch(
                    utcnow(),
                    profile.preferences.maximum_application_age_hours,
                    include_stale,
                    args.reanalyze or args.outdated_only,
                    args.limit,
                    source=args.source,
                ).with_only_columns(Job.id)
            ).all()
        )
        if args.outdated_only:
            current = profile_fingerprint(profile)
            job_ids = [j for j in job_ids if has_outdated_analysis(db, j, current)]
        if not job_ids:
            print("No jobs waiting for analysis (stale jobs are skipped unless --include-stale).")
            return 0
        try:
            run = create_analysis_run(
                db, f"Analyze & shortlist {len(job_ids)} jobs", settings.development_mode
            )
        except RunConflictError as exc:
            print(exc, file=sys.stderr)
            return 1
    print(f"Analyzing {len(job_ids)} jobs with {analyzer.name} (run {run.id})")
    result = AnalysisService(factory).execute(
        run.id,
        job_ids,
        AnalysisContext(
            analyzer=analyzer,
            profile=profile,
            rules=rules,
            replace_outdated=args.replace_outdated,
        ),
    )
    print(result.summary)
    return 0 if result.status.value == "COMPLETED" else 1


def cmd_runs(args: argparse.Namespace) -> int:
    with get_session_factory()() as db:
        for run in db.scalars(
            select(AgentRun).order_by(AgentRun.started_at.desc()).limit(args.limit)
        ):
            print(
                f"{run.started_at:%Y-%m-%d %H:%M} {run.status.value:<9} {run.run_type.value:<9} "
                f"{run.id}  {run.summary or run.task}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252; results contain characters like the rupee sign.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    configure_logging(get_settings())
    parser = argparse.ArgumentParser(prog="python -m backend.scripts.jobs")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("discover", help="discover jobs from configured sources")
    d.add_argument("--source", help="source name from job_sources.yaml (default: all enabled)")
    d.set_defaults(func=cmd_discover)
    a = sub.add_parser("analyze", help="analyze waiting jobs and apply shortlist rules")
    a.add_argument("--limit", type=int, default=25)
    a.add_argument("--include-stale", action="store_true")
    a.add_argument("--reanalyze", action="store_true", help="also re-analyze analyzed jobs")
    a.add_argument("--source", help="only jobs from this source (e.g. greenhouse)")
    a.add_argument(
        "--outdated-only",
        action="store_true",
        help="only jobs never analyzed, or last analyzed with a different profile",
    )
    a.add_argument(
        "--replace-outdated",
        action="store_true",
        help="after a successful re-analysis, delete analyses made with an older profile",
    )
    a.set_defaults(func=cmd_analyze)
    r = sub.add_parser("runs", help="list recent runs")
    r.add_argument("--limit", type=int, default=10)
    r.set_defaults(func=cmd_runs)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
