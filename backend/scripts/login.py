"""Log in to a job platform ONCE, by hand, so PIE can reuse the session (REQUIREMENTS 4).

    python -m backend.scripts.login naukri                 # log in, then check
    python -m backend.scripts.login naukri --check         # only re-check the saved session
    python -m backend.scripts.login naukri --test-search   # also try one search page
    python -m backend.scripts.login linkedin --test-search
    python -m backend.scripts.login instahyre

A normal, visible browser opens with PIE's own profile for that platform (data/browser/<name>/,
git-ignored). You type your login and OTP yourself; PIE never sees or stores your password.
Only the outcome (logged in / expired / blocked) is saved, for the dashboard. If the platform
refuses PIE's browser, PIE reports it and stops: it never tries to disguise itself.
"""

import argparse
import sys
from typing import Any

from backend.core.config import REPO_ROOT
from integrations.platforms.base import LoginCheck, classify_page, save_status
from integrations.platforms.instahyre import selectors as instahyre
from integrations.platforms.linkedin import selectors as linkedin
from integrations.platforms.naukri import selectors as naukri

PLATFORMS: dict[str, Any] = {"naukri": naukri, "linkedin": linkedin, "instahyre": instahyre}
DATA_DIR = REPO_ROOT / "data"


def _open(platform: str) -> tuple[Any, Any, Any]:
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    profile = DATA_DIR / "browser" / platform
    profile.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for channel in ("msedge", "chrome", None):
        kwargs: dict[str, Any] = {"headless": False}
        if channel:
            kwargs["channel"] = channel
        try:
            context = pw.chromium.launch_persistent_context(str(profile), **kwargs)
            return pw, context, context.pages[0] if context.pages else context.new_page()
        except Exception as exc:  # try the next installed browser
            last = exc
    pw.stop()
    raise SystemExit(f"No browser could be opened: {last}")


def _check(page: Any, sel: Any, url: str) -> LoginCheck:
    response = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    page.wait_for_timeout(3000)
    text = page.inner_text("body") if page.locator("body").count() else ""
    return classify_page(sel, page.url, response.status if response else None, text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("platform", choices=sorted(PLATFORMS))
    parser.add_argument("--check", action="store_true", help="only re-check the saved session")
    parser.add_argument("--test-search", action="store_true", help="also open one search page")
    args = parser.parse_args(argv)
    sel = PLATFORMS[args.platform]

    pw, context, page = _open(args.platform)
    try:
        if not args.check:
            page.goto(sel.LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
            print(f"\nA browser window opened at the {sel.NAME} login page.")
            print("Log in there yourself (password and OTP). PIE does not see or store them.")
            input("When you are logged in, come back here and press Enter... ")
        check = _check(page, sel, sel.HOME_URL)
        save_status(DATA_DIR, args.platform, check)
        print(f"\nLogin check: {check.state.upper()} - {check.detail}")
        if check.state == "logged_in" and args.test_search:
            search = _check(page, sel, sel.search_url("Python Developer", "Gurugram"))
            if search.state == "blocked":
                save_status(DATA_DIR, args.platform, search)
                print(f"Search page: BLOCKED - {search.detail}")
                print(f"PIE will not try to get around this. {sel.NAME} stays manual-apply.")
            else:
                cards = page.locator(sel.JOB_CARD).count()
                print(f"Search page: loaded, {cards} job card(s) found with the current selectors.")
                if cards == 0:
                    print("No cards matched: the selectors in selectors.py need checking.")
        return 0 if check.state == "logged_in" else 1
    finally:
        context.close()
        pw.stop()


if __name__ == "__main__":
    sys.exit(main())
