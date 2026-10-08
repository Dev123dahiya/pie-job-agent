"""One visible browser per platform, using PIE's saved login profile for it."""

import threading
from typing import Any

from backend.core.config import REPO_ROOT

_managers: dict[str, Any] = {}
_lock = threading.Lock()


def get_platform_browser(platform: str, settings: Any) -> Any:
    """Process-wide BrowserManager on data/browser/<platform>/ (the profile the user logged
    in with). Always visible: these sites are used the way a person uses them."""
    from integrations.browser.manager import BrowserManager

    with _lock:
        if platform not in _managers:
            _managers[platform] = BrowserManager(
                channel=settings.browser_channel,
                headless=False,
                timeout_ms=max(settings.browser_timeout_ms, 45_000),
                user_data_dir=REPO_ROOT / "data" / "browser" / platform,
            )
        return _managers[platform]
