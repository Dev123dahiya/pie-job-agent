"""Browser automation (Playwright) behind PIE's permission-checked application tools."""

from integrations.browser.manager import BrowserError, BrowserManager, Snapshot, get_browser_manager

__all__ = ["BrowserError", "BrowserManager", "Snapshot", "get_browser_manager"]
