"""Daily discovery + analysis from the command line.

python -m backend.scripts.daily run      # find + score new jobs now, print the report
python -m backend.scripts.daily apply    # AUTO-apply now (real applications), every platform
python -m backend.scripts.daily apply --platform linkedin   # ...or one platform
python -m backend.scripts.daily all      # run, then apply: the whole daily run, now
python -m backend.scripts.daily status   # schedule and the latest daily run

The same happens automatically once a day inside the API process (DAILY_RUN_ENABLED,
DAILY_RUN_TIME, DAILY_AUTO_APPLY). "apply" sends REAL applications on every platform where
auto mode is unlocked, with all of PIE's rules: the live switches in .env, daily limits,
duplicate checks, the stop on site warnings, and a pause for any question without a saved
answer (answer those on the Needs my input page). Typing the command is the confirmation.
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


def cmd_apply(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from agent.tools import build_default_registry
    from backend.core.profile import load_user_profile
    from backend.services.daily_auto import DAILY_AUTO_PLATFORMS, run_daily_auto
    from backend.services.limits import usage
    from database.models import BrowserSession, Job
    from integrations.platforms.browser import get_platform_browser

    settings = get_settings()
    factory = get_session_factory()
    platforms = (args.platform,) if args.platform else DAILY_AUTO_PLATFORMS
    opened: dict[str, Any] = {}

    def browser(platform: str) -> Any:
        opened[platform] = get_platform_browser(platform, settings)
        return opened[platform]

    try:
        out = run_daily_auto(
            factory, settings, load_user_profile(), build_default_registry(), browser,
            platforms=platforms, standing_confirmation=False,
        )  # fmt: skip
    finally:
        for b in opened.values():
            b.shutdown()
    with factory() as db:
        for platform, result in out.items():
            if not isinstance(result, dict) or not result.get("run_id"):
                print(
                    f"{platform}: {result.get('skipped') if isinstance(result, dict) else result}"
                )
                continue
            print(f"{platform}: {result['counts']}")
            sessions = db.scalars(
                select(BrowserSession).where(BrowserSession.run_id == result["run_id"])
            ).all()
            for s in sessions:
                job = db.get(Job, s.job_id)
                if job is None:
                    continue
                note = "; ".join(m.get("question", "") for m in s.missing_answers or [])
                company = job.company.name if job.company else "?"
                asks = f"  (asks: {note})" if note else ""
                print(f"  - {s.state.value:<17} {job.title} @ {company}{asks}")
        today = usage(db, settings.automation_path, settings.timezone)
        used = {u.platform: f"{u.used}/{u.limit}" for u in today}
    print(f"Today: {used}")
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    code = cmd_run(args)
    if code != 0:
        return code
    return cmd_apply(args)


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
    a = sub.add_parser("apply", help="AUTO-apply now (real applications) where auto is unlocked")
    a.add_argument("--platform", choices=["naukri", "linkedin", "instahyre"])
    a.set_defaults(func=cmd_apply)
    al = sub.add_parser("all", help="find + score new jobs, then auto-apply (the daily run, now)")
    al.add_argument("--json", action="store_true", help="print the full report as JSON")
    al.add_argument("--platform", choices=["naukri", "linkedin", "instahyre"])
    al.set_defaults(func=cmd_all)
    s = sub.add_parser("status", help="show the schedule and the latest daily run")
    s.set_defaults(func=cmd_status)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
