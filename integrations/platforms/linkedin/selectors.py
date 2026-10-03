"""Everything LinkedIn-specific PIE relies on: URLs, text markers and page selectors.

The user chose to automate LinkedIn Easy Apply knowing LinkedIn's User Agreement does not
allow it and that LinkedIn may restrict the account (decision recorded 2026-10-04 in
docs/PROGRESS.md). PIE keeps LinkedIn's volume lowest (daily limit 5, below Naukri), works only
in a visible browser with the user's own login, and stops at any security check.

When LinkedIn changes its site, this is the one file to update. Items marked VERIFY have not
yet been checked against the live, logged-in site.
"""

from urllib.parse import urlencode

NAME = "LinkedIn"
LOGIN_URL = "https://www.linkedin.com/login"
HOME_URL = "https://www.linkedin.com/feed/"  # only a logged-in user sees the feed
# Logged out, or a security check ("checkpoint") only the user can pass.
LOGGED_OUT_URL_PARTS = ("/login", "/authwall", "/checkpoint", "/uas/login", "/signup")
# VERIFY: text the logged-in feed shows (lower case).
LOGGED_IN_MARKERS = ("start a post", "my network", "messaging")
# LinkedIn answers suspected automation with HTTP 999 or a "request denied" page.
BLOCKED_MARKERS = ("request denied", "unusual activity", "we've restricted your account")


def search_url(keyword: str, location: str) -> str:
    """Jobs search filtered to Easy Apply (f_AL=true), newest first (sortBy=DD)."""
    query = urlencode({"keywords": keyword, "location": location, "f_AL": "true", "sortBy": "DD"})
    return f"https://www.linkedin.com/jobs/search/?{query}"


# --- search results (VERIFY all) ----------------------------------------------------------
JOB_CARD = "div.job-card-container"
CARD_TITLE = "a.job-card-list__title, a.job-card-container__link"
CARD_COMPANY = ".artdeco-entity-lockup__subtitle"
CARD_LOCATION = ".job-card-container__metadata-item"

# --- Easy Apply (VERIFY all) ----------------------------------------------------------------
EASY_APPLY_BUTTON = "button.jobs-apply-button"
MODAL = "div.jobs-easy-apply-modal"
NEXT_BUTTON = "button[aria-label='Continue to next step']"
REVIEW_BUTTON = "button[aria-label='Review your application']"
SUBMIT_BUTTON = "button[aria-label='Submit application']"
MAX_STEPS = 8  # REQUIREMENTS 9: multi-step forms are capped; stuck = failed
