"""Everything Instahyre-specific PIE relies on: URLs, text markers and page selectors.

When Instahyre changes its site, this is the one file to update. Items marked VERIFY have not
yet been checked against the live, logged-in site.
"""

from integrations.platforms.base import slug

NAME = "Instahyre"
LOGIN_URL = "https://www.instahyre.com/login/"
# VERIFY: the candidate's job feed, which only a logged-in user sees.
HOME_URL = "https://www.instahyre.com/candidate/opportunities/"
LOGGED_OUT_URL_PARTS = ("/login",)
# VERIFY: text the logged-in feed shows (lower case).
LOGGED_IN_MARKERS = ("opportunities", "logout", "my profile")
BLOCKED_MARKERS = ("access denied", "you don't have permission to access")


def search_url(keyword: str, location: str) -> str:
    """VERIFY: e.g. https://www.instahyre.com/python-developer-jobs-in-gurugram/"""
    return f"https://www.instahyre.com/{slug(keyword)}-jobs-in-{slug(location)}/"
