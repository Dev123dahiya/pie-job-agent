"""C1/C2: login checks and the login status store (no browser needed)."""

from pathlib import Path

from integrations.platforms.base import LoginCheck, classify_page, load_status, save_status
from integrations.platforms.linkedin import selectors as linkedin
from integrations.platforms.naukri import selectors as naukri


def test_naukri_page_classification() -> None:
    home = naukri.HOME_URL
    assert classify_page(naukri, home, 200, "Welcome back. View profile").state == "logged_in"
    assert classify_page(naukri, naukri.LOGIN_URL, 200, "Login").state == "expired"
    denied = "Access Denied You don't have permission to access ... errors.edgesuite.net"
    assert classify_page(naukri, home, 403, denied).state == "blocked"
    assert classify_page(naukri, home, 200, denied).state == "blocked"  # 200 with a denial page
    assert classify_page(naukri, home, 200, "Something else").state == "expired"  # never assumed


def test_search_url() -> None:
    url = naukri.search_url("Python Developer", "Delhi NCR")
    assert url == "https://www.naukri.com/python-developer-jobs-in-delhi-ncr"


def test_status_store_keeps_only_the_outcome(tmp_path: Path) -> None:
    assert load_status(tmp_path) == {}
    save_status(tmp_path, "naukri", LoginCheck("logged_in", "Logged in to Naukri.", "https://x"))
    saved = load_status(tmp_path)
    assert set(saved["naukri"]) == {"state", "detail", "checked_at"}  # no cookies, no URLs
    assert saved["naukri"]["state"] == "logged_in"
    (tmp_path / "platform_logins.json").write_text("not json", encoding="utf-8")
    assert load_status(tmp_path) == {}


def test_linkedin_page_classification_and_search_url() -> None:
    feed = linkedin.HOME_URL
    assert classify_page(linkedin, feed, 200, "Start a post ... Messaging").state == "logged_in"
    login = "https://www.linkedin.com/login?session_redirect=x"
    assert classify_page(linkedin, login, 200, "Sign in").state == "expired"
    check = "https://www.linkedin.com/checkpoint/challenge/abc"
    assert classify_page(linkedin, check, 200, "Let's do a quick security check").state == "expired"
    assert classify_page(linkedin, feed, 999, "").state == "blocked"
    url = linkedin.search_url("Python Developer", "Gurugram")
    assert url.startswith("https://www.linkedin.com/jobs/search/?") and "f_AL=true" in url
    assert "keywords=Python+Developer" in url
    assert linkedin.MAX_STEPS <= 10
