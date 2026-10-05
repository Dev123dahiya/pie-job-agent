"""Everything Cutshort-specific PIE relies on: URLs, text markers and page selectors.

When Cutshort changes its site, this is the one file to update. Items marked VERIFY have not
yet been checked against the live, logged-in site. Checked 2026-10-05 without a login: there
is no /login page (login is a dialog on the home page) and /profile redirects a logged-out
visitor to "/?redirect_url=%2Fprofile".
"""

NAME = "Cutshort"
PLATFORM = "cutshort"
DOMAIN = "cutshort.io"
LOGIN_URL = "https://cutshort.io/"
HOME_URL = "https://cutshort.io/profile"  # only a logged-in user stays on this page
LOGGED_OUT_URL_PARTS = ("redirect_url=",)
# VERIFY: text the logged-in profile page shows (lower case).
LOGGED_IN_MARKERS = ("logout", "log out", "edit profile", "my profile", "your profile")
BLOCKED_MARKERS = ("access denied", "you don't have permission to access")


def search_url(keyword: str, location: str) -> str:
    """VERIFY: Cutshort's job search page."""
    return "https://cutshort.io/jobs"
