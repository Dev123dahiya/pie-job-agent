"""Everything Indeed-specific PIE relies on: URLs, text markers and page selectors.

When Indeed changes its site, this is the one file to update. Checked on the live,
logged-in site 2026-10-05:
- login page secure.indeed.com/auth; My Jobs redirects a logged-out visitor to /account/login;
  plain (non-browser) requests get HTTP 403;
- the search page (in.indeed.com/jobs?q=..&l=..&fromage=<days>&sort=date) embeds its results
  in window.mosaic.providerData["mosaic-provider-jobcards"] (jobkey, pubDate in ms,
  indeedApplyEnabled, title, company, location, snippet, salary): PIE reads that data;
- job pages (viewjob?jk=..) sometimes answer with a "Just a moment..." bot check (HTTP 403).
  PIE never tries to get around it: it just skips that job's full description.
"""

from urllib.parse import urlencode

NAME = "Indeed"
PLATFORM = "indeed"
DOMAIN = "indeed.com"
LOGIN_URL = "https://secure.indeed.com/auth"
HOME_URL = "https://myjobs.indeed.com/"  # only a logged-in user stays on this page
LOGGED_OUT_URL_PARTS = ("/account/login", "secure.indeed.com/auth")
# VERIFY: text the logged-in My Jobs page shows (lower case).
LOGGED_IN_MARKERS = ("my jobs", "saved", "applied", "sign out")
# Indeed's bot check. PIE never tries to get around it: it stops, and the user can solve it.
BLOCKED_MARKERS = (
    "access denied",
    "verify you are human",
    "verifying you are human",
    "additional verification required",
    "request blocked",
    "just a moment...",
)
BASE_URL = "https://in.indeed.com"
# Where the search page keeps its results (property names, walked by fixed code).
RESULTS_PATH = [
    "mosaic", "providerData", "mosaic-provider-jobcards", "metaData",
    "mosaicProviderJobCardsModel", "results",
]  # fmt: skip
# PIE's location names -> what Indeed understands.
LOCATIONS = {"gurugram": "Gurugram, Haryana", "delhi ncr": "Delhi", "remote": "remote"}


def search_url(keyword: str, location: str, max_age_days: int | None = None) -> str:
    """Indeed India's search page, newest first; ``fromage`` keeps jobs from the last N days."""
    params: dict[str, str | int] = {
        "q": keyword,
        "l": LOCATIONS.get(location.strip().lower(), location.strip()),
        "sort": "date",
    }
    if max_age_days:
        params["fromage"] = max_age_days
    return f"{BASE_URL}/jobs?" + urlencode(params)


def job_url(job_key: str) -> str:
    return f"{BASE_URL}/viewjob?" + urlencode({"jk": job_key})
