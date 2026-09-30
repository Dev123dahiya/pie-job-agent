"""Daily discovery + analysis from the command line.

python -m backend.scripts.daily run      # run it now (manual trigger), print the report
python -m backend.scripts.daily status   # schedule and the latest daily run

The same run happens automatically once a day inside the API process (DAILY_RUN_ENABLED,
DAILY_RUN_TIME). Discovery and analysis only: no applications, no browser, no submissions.
"""

import argparse
import json
import sys
from typing import Any
from zoneinfo import ZoneInfo

from backend.core.config import get_settings
from backend.core.logging import configure_logging
from backend.services.daily import (
    COUNT_KEYS,
    build_daily_plan,
    latest_daily_report,
    next_slot,
    parse_run_time,
    run_daily,
)
from backend.services.runs import RunConflictError
from database.base import utcnow
from database.session import get_session_factory


def _print_report(report: dict[str, Any]) -> None:
    counts = report.get("counts") or {}
    print("Counts: " + ", ".join(f"{k}={counts.get(k, 0)}" for k in COUNT_KEYS))
    for name, step in (report.get("per_source") or {}).items():
        print(
            f"  {name:<16} {step.get('status', '?'):<9} discovered={step.get('discovered', 0)} "
            f"new={step.get('new', 0)} dup={step.get('duplicates', 0)} "
            f"fresh={step.get('fresh', 0)} stale={step.get('stale', 0)} "
            f"unknown={step.get('unknown', 0)}"
            + (f"  error: {step['error']}" if step.get("error") else "")
        )
    analysis = report.get("analysis") or {}
    print(f"Analysis: {json.dumps(analysis, default=str)}")
    for key, label in (("newly_shortlisted", "SHORTLISTED"), ("newly_ready_to_apply", "READY")):
        jobs = report.get(key) or []
        print(f"Newly {label}: {len(jobs)}")
        for j in jobs:
            print(f"  - {j['title']} @ {j.get('company') or '?'} ({j.get('location') or '?'})")
    for failure in report.get("failures") or []:
        print(f"Failure [{failure['step']}]: {failure['error']}")
    if "safety" in report:
        print(f"Safety: {json.dumps(report['safety'])}")


def cmd_run(args: argparse.Namespace) -> int:
    settings = get_settings()
    plan = build_daily_plan(settings)
    if not plan.sources:
        print("No sources enabled in config/job_sources.yaml", file=sys.stderr)
        return 1
    try:
        run_id, report = run_daily(get_session_factory(), plan, "manual")
    except RunConflictError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Daily run {run_id}")
    if args.json:
        print(json.dumps(report, indent=1, default=str, ensure_ascii=False))
    else:
        _print_report(report)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = get_settings()
    tz = ZoneInfo(settings.timezone)
    if settings.daily_run_enabled:
        at = next_slot(utcnow().astimezone(tz), parse_run_time(settings.daily_run_time))
        when = f"{settings.daily_run_time} {settings.timezone}"
        print(f"Schedule: daily at {when} (next {at:%Y-%m-%d %H:%M})")
    else:
        print("Schedule: disabled (DAILY_RUN_ENABLED=false)")
    with get_session_factory()() as db:
        found = latest_daily_report(db)
    if found is None:
        print("No daily run yet.")
        return 0
    run, report = found
    print(f"Latest: {run.id} {run.status.value} started {run.started_at:%Y-%m-%d %H:%M} UTC")
    print(run.summary or "")
    if report:
        _print_report(report)
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging(get_settings())
    parser = argparse.ArgumentParser(prog="python -m backend.scripts.daily")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run daily discovery + analysis now")
    r.add_argument("--json", action="store_true", help="print the full report as JSON")
    r.set_defaults(func=cmd_run)
    s = sub.add_parser("status", help="show the schedule and the latest daily run")
    s.set_defaults(func=cmd_status)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
