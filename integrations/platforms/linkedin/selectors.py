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
# A job page LinkedIn shows to a signed-out visitor (seen 2026-10-05: "Join LinkedIn" with
# "Agree & Join" instead of the job).
LOGGED_OUT_TEXTS = ("agree & join", "join linkedin", "sign in to see who you already know")
# VERIFY: text the logged-in feed shows (lower case).
LOGGED_IN_MARKERS = ("start a post", "my network", "messaging")
# LinkedIn answers suspected automation with HTTP 999 or a "request denied" page.
BLOCKED_MARKERS = ("request denied", "unusual activity", "we've restricted your account")


def search_url(keyword: str, location: str) -> str:
    """Jobs search filtered to Easy Apply (f_AL=true), newest first (sortBy=DD)."""
    query = urlencode({"keywords": keyword, "location": location, "f_AL": "true", "sortBy": "DD"})
    return f"https://www.linkedin.com/jobs/search/?{query}"


# --- search results (checked on the live, logged-in page 2026-10-04) ------------------------
# 25 list items per page carry the job id; LinkedIn draws a card's contents only once it is
# scrolled into view, so every item is scrolled to before reading.
JOB_CARD = "li[data-occludable-job-id]"
CARD_SPEC = {
    "card": JOB_CARD,
    "id_attr": "data-occludable-job-id",
    "fields": {
        "title": {"css": "a.job-card-container__link", "attr": "aria-label"},
        "company": {"css": ".artdeco-entity-lockup__subtitle"},
        "location": {"css": ".job-card-container__metadata-wrapper li"},
        "posted": {"css": "time", "attr": "datetime"},  # VERIFY: not on every card
        "footer": {"css": ".job-card-list__footer-wrapper"},
    },
}


def job_url(job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def experience_levels(years: float | None) -> str | None:
    """LinkedIn's experience filter (f_E: 1 internship, 2 entry, 3 associate, 4 mid-senior)
    for the user's total experience, so senior roles are not even listed."""
    if years is None:
        return None
    if years < 1:
        return "1,2"
    if years < 3:
        return "2,3"
    return "3,4"


TITLE_NOISE = (" with verification",)  # the verified-badge text inside the title link


def search_page_url(
    keyword: str,
    location: str,
    page: int,
    max_age_hours: int | None,
    experience_years: float | None = None,
) -> str:
    """25 per page (start=0, 25, ...); f_TPR limits results to the freshness window, f_E to
    the user's experience level."""
    params = {"keywords": keyword, "location": location, "f_AL": "true", "sortBy": "DD"}
    if max_age_hours:
        params["f_TPR"] = f"r{max_age_hours * 3600}"
    levels = experience_levels(experience_years)
    if levels:
        params["f_E"] = levels
    if page > 1:
        params["start"] = str((page - 1) * 25)
    return f"https://www.linkedin.com/jobs/search/?{urlencode(params)}"


# --- Easy Apply (checked on the live page 2026-10-04) --------------------------------------
# The job page's button has aria-label "Easy Apply to this job". The form opens on the same
# page as a panel headed "Apply to <company>" (no role=dialog; style class names are
# scrambled), with buttons found by their text: Dismiss (aria), Back, Next, Review, Submit
# application. LinkedIn keeps a started application and reopens it at the step it was left.
EASY_APPLY_BUTTON_NAME = "Easy Apply to this job"
PANEL_HEADING_PREFIX = "Apply to "
NEXT_TEXT = "Next"
REVIEW_TEXT = "Review"
SUBMIT_TEXT = "Submit application"  # the ONE irreversible click; review mode + "yes" only
DISMISS_LABEL = "Dismiss"
DISCARD_TEXT = "Discard"
SENT_TEXTS = ("application was sent", "application sent")  # VERIFY on the first submit
MAX_STEPS = 8  # REQUIREMENTS 9: multi-step forms are capped; stuck = failed

# A daily quota or warning message: PIE stops on this platform at once (REQUIREMENTS 10).
# VERIFY: the exact wording appears only when it happens; these patterns are deliberately broad.
QUOTA_PATTERN = (
    r"(daily|today'?s?)\s+(apply|application|easy apply)\s+limit"
    r"|(reached|hit|exceeded)\b[^.\n]{0,40}\b(apply|application|daily)\b[^.\n]{0,20}\blimit"
    r"|limit\s+(of\s+)?(applies|applications)\s+(for\s+)?today"
    r"|unusual activity|temporarily restricted|we've restricted your account"
)
