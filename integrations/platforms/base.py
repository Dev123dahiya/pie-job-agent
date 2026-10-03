"""The common interface every job platform implements (REQUIREMENTS 3, 4).

    login check -> search_jobs -> get_job_details -> apply

Logins (REQUIREMENTS 4): the user logs in once, by hand (OTP included), in a normal visible
browser that uses PIE's own profile folder for that platform (data/browser/<platform>/,
git-ignored). PIE reuses that session; it never sees, stores or types a password and never
logs in by itself. If a session expires the platform is paused until the user logs in again.

Bot protection: if a platform refuses PIE's browser (e.g. "403 Access Denied"), PIE stops on
that platform and reports it. It never tries to look like something else (no stealth plugins,
fake fingerprints or user-agent tricks).
"""

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

LoginState = Literal["logged_in", "expired", "never", "blocked"]


@dataclass(frozen=True)
class LoginCheck:
    state: LoginState
    detail: str
    url: str | None = None


class PlatformSelectors(Protocol):
    """Each platform keeps every URL, text marker and page selector in ONE file."""

    NAME: str
    LOGIN_URL: str
    HOME_URL: str  # a page only a logged-in user sees
    LOGGED_OUT_URL_PARTS: tuple[str, ...]
    LOGGED_IN_MARKERS: tuple[str, ...]  # text on HOME_URL when logged in (lower case)
    BLOCKED_MARKERS: tuple[str, ...]  # bot-protection pages (lower case)

    @staticmethod
    def search_url(keyword: str, location: str) -> str: ...


class JobPlatform(Protocol):
    name: str

    def check_login(self, page: Any) -> LoginCheck: ...
    def search_jobs(self, page: Any, keyword: str, location: str) -> list[dict[str, Any]]: ...
    def get_job_details(self, page: Any, url: str) -> dict[str, Any]: ...
    def apply(self, page: Any, url: str, *, dry_run: bool) -> dict[str, Any]: ...


def classify_page(sel: Any, url: str, status: int | None, text: str) -> LoginCheck:
    """Logged in, logged out or blocked, from what a page shows. Pure: easy to test.
    ``sel`` is a platform's selectors module (shaped like PlatformSelectors)."""
    low = text.lower()
    if status in (401, 403, 429, 999) or any(m in low for m in sel.BLOCKED_MARKERS):
        return LoginCheck("blocked", f"{sel.NAME} refused PIE's browser (HTTP {status}).", url)
    if any(part in url.lower() for part in sel.LOGGED_OUT_URL_PARTS):
        return LoginCheck("expired", f"Not logged in to {sel.NAME}: log in again.", url)
    if any(m in low for m in sel.LOGGED_IN_MARKERS):
        return LoginCheck("logged_in", f"Logged in to {sel.NAME}.", url)
    return LoginCheck("expired", f"Could not confirm a {sel.NAME} login on this page.", url)


# --- login status store (no cookies, no passwords: only the outcome of the last check) ---


def status_file(data_dir: Path) -> Path:
    return data_dir / "platform_logins.json"


def save_status(data_dir: Path, platform: str, check: LoginCheck) -> None:
    path = status_file(data_dir)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    data[platform] = {
        "state": check.state,
        "detail": check.detail[:300],
        "checked_at": datetime.now(UTC).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def load_status(data_dir: Path) -> dict[str, dict[str, Any]]:
    path = status_file(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict) and v.get("state")}


def slug(text: str) -> str:
    """'Python Developer' -> 'python-developer' (for search URLs)."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def as_dict(check: LoginCheck) -> dict[str, Any]:
    return asdict(check)


_AGE = re.compile(
    r"(?P<n>\d+)\+?\s*(?P<unit>minute|min|hour|hr|day|week|month)s?\s+ago", re.IGNORECASE
)
_RECENT = re.compile(r"\b(just now|today|few hours ago|moments ago|now)\b", re.IGNORECASE)
_UNIT_HOURS = {"minute": 1 / 60, "min": 1 / 60, "hour": 1, "hr": 1, "day": 24, "week": 168,
               "month": 720}  # fmt: skip


def parse_relative_age(text: str, seen: datetime) -> datetime | None:
    """The OLDEST time a relative age can mean, so a job never looks fresher than it is:
    "2 days ago" -> 3 days before ``seen``; "5 hours ago" -> 6 hours; "today" -> 24 hours;
    "30+ days ago" -> 31 days. None when there is no age in the text."""
    from datetime import timedelta

    m = _AGE.search(text or "")
    if m:
        unit = m["unit"].lower()
        hours = (int(m["n"]) + 1) * _UNIT_HOURS[unit]
        return seen - timedelta(hours=hours)
    if _RECENT.search(text or ""):
        return seen - timedelta(hours=24)
    return None
