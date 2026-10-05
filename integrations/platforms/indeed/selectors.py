"""Everything Indeed-specific PIE relies on: URLs, text markers and page selectors.

When Indeed changes its site, this is the one file to update. Items marked VERIFY have not
yet been checked against the live, logged-in site. Checked 2026-10-05 without a login: the
login page is secure.indeed.com/auth, and My Jobs redirects a logged-out visitor to
/account/login. Indeed answers plain (non-browser) requests with HTTP 403.
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
    "additional verification required",
    "request blocked",
)


def search_url(keyword: str, location: str) -> str:
    """VERIFY: Indeed India's search page."""
    return "https://in.indeed.com/jobs?" + urlencode({"q": keyword, "l": location})
