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
CARD_SALARY = "span.sal-wrap"
CARD_SNIPPET = "span.job-desc"
CARD_TAGS = "ul.tags-gt"

# Checked on the live, logged-in search page 2026-10-04: 20 cards per page, each with a
# data-job-id; the title link is the job's own page.
CARD_SPEC = {
    "card": JOB_CARD,
    "id_attr": "data-job-id",
    "fields": {
        "title": {"css": CARD_TITLE},
        "url": {"css": CARD_TITLE, "attr": "href"},
        "company": {"css": CARD_COMPANY},
        "experience": {"css": CARD_EXPERIENCE},
        "salary": {"css": CARD_SALARY},
        "location": {"css": CARD_LOCATION},
        "snippet": {"css": CARD_SNIPPET},
        "tags": {"css": CARD_TAGS},
        "posted": {"css": CARD_POSTED},
    },
}


def search_page_url(keyword: str, location: str, page: int, experience_years: int | None) -> str:
    """Page 1 is the plain URL, page N appends -N. ``experience`` makes Naukri itself drop
    jobs clearly outside the user's experience (REQUIREMENTS 5)."""
    url = search_url(keyword, location) + (f"-{page}" if page > 1 else "")
    return url + (f"?experience={experience_years}" if experience_years is not None else "")


# --- job page: how to apply (checked on the live job pages 2026-10-04) -------------------
# Either button appears (twice: top and bottom of the page). "Apply" sends the profile on
# Naukri at once (no form first); "Apply on company site" opens the employer's own site.
APPLY_BUTTON = "#apply-button"
COMPANY_SITE_BUTTON = "#company-site-button"
APPLY_SPEC = {
    "card": f"{APPLY_BUTTON}, {COMPANY_SITE_BUTTON}",
    "id_attr": "id",
    "fields": {"text": {"css": None}},
}
# After clicking Apply (VERIFY on the first real application): the button reads "Applied",
# or a chatbot drawer asks follow-up questions.
APPLIED_TEXTS = ("applied", "you have successfully applied", "application sent")
FOLLOW_UP_MARKER = "[class*='chatbot']"
