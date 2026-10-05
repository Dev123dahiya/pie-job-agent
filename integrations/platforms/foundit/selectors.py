"""Everything foundit-specific PIE relies on: URLs, text markers and page selectors.

When foundit changes its site, this is the one file to update. Items marked VERIFY have not
yet been checked against the live, logged-in site.
"""

NAME = "foundit"
PLATFORM = "foundit"
DOMAIN = "foundit.in"
LOGIN_URL = "https://www.foundit.in/rio/login"
# VERIFY: a page only a logged-in job seeker sees.
HOME_URL = "https://www.foundit.in/seeker/dashboard"
LOGGED_OUT_URL_PARTS = ("/login",)
# VERIFY: text the logged-in page shows (lower case).
LOGGED_IN_MARKERS = ("logout", "log out", "my profile", "profile completeness", "recommended jobs")
BLOCKED_MARKERS = ("access denied", "you don't have permission to access")


def search_url(keyword: str, location: str) -> str:
    """VERIFY: foundit's search page."""
    from urllib.parse import urlencode

    return "https://www.foundit.in/srp/results?" + urlencode(
        {"query": keyword, "locations": location}
    )
