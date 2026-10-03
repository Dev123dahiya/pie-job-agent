"""Everything Naukri-specific PIE relies on: URLs, text markers and page selectors.

When Naukri changes its site, this is the one file to update. Items marked VERIFY have not
yet been checked against the live, logged-in site (that needs the user's own login; see
docs/PROGRESS.md "Blockers").
"""

from integrations.platforms.base import slug

NAME = "Naukri"
LOGIN_URL = "https://www.naukri.com/nlogin/login"
HOME_URL = "https://www.naukri.com/mnjuser/homepage"  # only a logged-in user sees this
LOGGED_OUT_URL_PARTS = ("/nlogin", "/login")
# VERIFY: text the logged-in homepage shows (lower case).
LOGGED_IN_MARKERS = ("view profile", "complete profile", "profile performance", "logout")
# Akamai's refusal page (seen 2026-10-03 on an automated headless browser).
BLOCKED_MARKERS = ("access denied", "you don't have permission to access", "errors.edgesuite.net")


def search_url(keyword: str, location: str) -> str:
    """e.g. https://www.naukri.com/python-developer-jobs-in-gurugram"""
    return f"https://www.naukri.com/{slug(keyword)}-jobs-in-{slug(location)}"


# --- search results page (VERIFY all) ---------------------------------------------------
JOB_CARD = "div.srp-jobtuple-wrapper"
CARD_TITLE = "a.title"
CARD_COMPANY = "a.comp-name"
CARD_EXPERIENCE = "span.expwdth"
CARD_LOCATION = "span.locWdth"
CARD_POSTED = "span.job-post-day"
