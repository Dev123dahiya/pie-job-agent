"""Everything foundit-specific PIE relies on: URLs, text markers and search parameters.

When foundit changes its site, this is the one file to update. Checked on the live,
logged-in site 2026-10-05:
- the search page (/srp/results?query=..&locations=..) loads its results with
  GET /middleware/jobsearch?sort=1&limit=..&query=..&locations=.. (limit 50 works);
- each result has createdAt (ms), min/max experience, and redirectUrl/applyUrl: almost all
  results are copies of LinkedIn jobs or company career pages; none could be applied to on
  foundit itself (5 searches, ~60 jobs). So foundit is a SEARCH-ONLY source: LinkedIn copies
  are dropped (PIE searches LinkedIn itself) and shortlisted jobs go to Manual apply;
- GET /middleware/jobdetail/<jobId> gives the full description. foundit's job pages
  (/job/...) answer HTTP 403 "Access Denied" to an automated browser: PIE never opens them.
"""

from urllib.parse import quote, urlencode

NAME = "foundit"
PLATFORM = "foundit"
DOMAIN = "foundit.in"
BASE_URL = "https://www.foundit.in"
LOGIN_URL = "https://www.foundit.in/rio/login"
# A page only a logged-in job seeker sees (the login check passed here 2026-10-05).
HOME_URL = "https://www.foundit.in/seeker/dashboard"
LOGGED_OUT_URL_PARTS = ("/login",)
LOGGED_IN_MARKERS = ("logout", "log out", "my profile", "profile completeness", "recommended jobs")
BLOCKED_MARKERS = ("access denied", "you don't have permission to access")

SEARCH_API_PREFIX = "/middleware/jobsearch?"
DETAIL_API_PREFIX = "/middleware/jobdetail/"
PAGE_SIZE = 50
# Copies of jobs from these sites are dropped: PIE searches them itself.
SKIP_APPLY_HOSTS = ("linkedin.com", "naukri.com", "instahyre.com")
# Results asking for more years than this are dropped before any analysis (saves Claude).
MAX_MIN_YEARS = 2


def search_url(keyword: str, location: str) -> str:
    """The search page (also the page PIE runs the search request from)."""
    return f"{BASE_URL}/srp/results?" + urlencode({"query": keyword, "locations": location})


def search_api_path(keyword: str, location: str, limit: int = PAGE_SIZE) -> str:
    """The same request foundit's search page sends."""
    return SEARCH_API_PREFIX + urlencode(
        {"sort": 1, "limit": limit, "query": keyword, "locations": location}, quote_via=quote
    )


def detail_api_path(job_id: str) -> str:
    return f"{DETAIL_API_PREFIX}{job_id}"
