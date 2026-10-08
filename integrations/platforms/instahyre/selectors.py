"""Everything Instahyre-specific PIE relies on: URLs, text markers and search parameters.

When Instahyre changes its site, this is the one file to update. Checked on the live,
logged-in site 2026-10-05:
- the opportunities page searches with GET /api/v1/job_search?skills=..&jobLocations=..&years=..
  (one location per request; "Gurgaon,Noida" is refused), 35 results per page, no date sort;
- every job has a public page (public_url) whose schema.org JobPosting data carries
  datePosted, and whose text shows the experience range ("1-3 Years") and "Apply now".
"""

from urllib.parse import urlencode

NAME = "Instahyre"
PLATFORM = "instahyre"
DOMAIN = "instahyre.com"
LOGIN_URL = "https://www.instahyre.com/login/"
# The candidate's job feed, which only a logged-in user sees.
HOME_URL = "https://www.instahyre.com/candidate/opportunities/"
LOGGED_OUT_URL_PARTS = ("/login",)
LOGGED_IN_MARKERS = ("opportunities", "sign out", "logout", "my profile")
BLOCKED_MARKERS = ("access denied", "you don't have permission to access")

SEARCH_API_PREFIX = "/api/v1/job_search?"
JOB_URL_PREFIX = "https://www.instahyre.com/job-"
PAGE_SIZE = 35

# PIE's location names -> Instahyre's. None = no location filter (all of India).
LOCATIONS: dict[str, str | None] = {
    "gurugram": "Gurgaon",
    "gurgaon": "Gurgaon",
    "noida": "Noida",
    "delhi ncr": "Delhi",
    "delhi": "Delhi",
    "remote": "Work From Home",
    "india": None,
    "bengaluru": "Bangalore",
    "bangalore": "Bangalore",
    "hyderabad": "Hyderabad",
    "pune": "Pune",
    "mumbai": "Mumbai",
    "chennai": "Chennai",
}


def instahyre_location(location: str) -> str | None:
    """Instahyre's name for a PIE location; unknown names are passed through as they are."""
    return LOCATIONS.get(location.strip().lower(), location.strip())


def search_api_path(term: str, location: str, years: int | None, offset: int = 0) -> str:
    """The same request the opportunities page sends for a search (status=0: open jobs)."""
    params: dict[str, str | int] = {"company_size": 0, "job_type": 0, "skills": term, "status": 0}
    place = instahyre_location(location)
    if place:
        params["jobLocations"] = place
    if years is not None:
        params["years"] = years
    if offset:
        params["offset"] = offset
    return SEARCH_API_PREFIX + urlencode(params)


def search_url(keyword: str, location: str) -> str:
    """For the login script's --test-search: the opportunities page itself (Instahyre has no
    shareable search address; PIE searches from this page)."""
    return HOME_URL


# --- job page: one-click apply (read from the page's own code, 2026-10-05) ----------------
# "Apply now" sends the Instahyre profile at once (no form, no questions): the page then shows
# a disabled "Application sent!" button with the date, as on every job already applied to.
APPLY_BUTTON = ".apply-button button"
APPLY_SPEC = {"card": APPLY_BUTTON, "id_attr": None, "fields": {"text": {"css": None}}}
APPLY_TEXT = "Apply now"
APPLIED_BUTTON_TEXT = "Application sent!"
APPLIED_PREFIX = "application sent"
COMPANY_SITE_ID: str | None = None  # Instahyre has no "apply on the company's site" button
APPLIED_TEXTS = ("application sent!", "application sent to")  # button, then the toast
FOLLOW_UP_MARKER = ".modal.in"  # never seen after "Apply now": any open dialog
# A daily quota or warning message: PIE stops on this platform at once (REQUIREMENTS 10).
QUOTA_PATTERN = (
    r"(daily|today'?s?)\s+(apply|application)\s+limit"
    r"|(reached|hit|exceeded)\b[^.\n]{0,40}\b(apply|application|daily)\b[^.\n]{0,20}\blimit"
    r"|unusual activity|temporarily restricted|account (has been )?(blocked|suspended)"
)
