"""Logging setup: console + rotating file under data/logs."""

import logging
import re
from logging.handlers import RotatingFileHandler

from backend.core.config import Settings

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

# What never reaches a log line (console, backend.log, run reports). Each pattern keeps the
# name and hides the value, so a log still says WHAT was there.
_REDACTED = "[REDACTED]"
_SECRET_NAMES = (
    r"password|passwd|pwd|passcode|otp|one[_-]?time[_-]?(?:code|password)|verification[_-]?code"
    r"|token|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|session[_-]?token"
    r"|secret|client[_-]?secret|api[_-]?key|apikey|app[_-]?key|app[_-]?id|x-api-key"
)
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Query parameters, e.g. Adzuna's ?app_id=...&app_key=...
    (
        re.compile(
            r"(?i)([?&](?:app_id|app_key|api_key|apikey|key|token|access_token|secret"
            r"|password)=)[^&\s\"']*"
        ),
        rf"\1{_REDACTED}",
    ),  # fmt: skip
    # Authorization headers: "Authorization: Bearer abc", {'authorization': 'Basic abc'}
    (
        re.compile(
            r"(?i)(\bauthorization[\"']?\s*[:=]\s*[\"']?)(?:(?:bearer|basic|token)\s+)?"
            r"[^\s\"',}]+"
        ),
        rf"\1{_REDACTED}",
    ),  # fmt: skip
    # Cookie / Set-Cookie headers: everything up to the end of the header value.
    (re.compile(r"(?i)(\b(?:set-)?cookie[\"']?\s*[:=]\s*[\"']?)[^\"'\r\n}]+"), rf"\1{_REDACTED}"),
    # name=value, name: value and "name": "value" for passwords, OTPs, tokens, keys...
    (
        re.compile(
            rf"(?i)(\b(?:{_SECRET_NAMES})[\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED\])"
            r"[^\s\"'&,;}]+"
        ),
        rf"\1{_REDACTED}",
    ),  # fmt: skip
    # API keys by their shape, wherever they appear (Anthropic, OpenAI-style, GitHub, AWS).
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{8,}"), f"sk-ant-{_REDACTED}"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), f"sk-{_REDACTED}"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), _REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), _REDACTED),
    # Passwords inside connection URLs: postgresql://user:password@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]+:)[^\s@/]+@"), rf"\1{_REDACTED}@"),
]
# The actual secret values from settings (.env), hidden wherever they appear, whatever the
# surrounding text looks like. Filled by configure_logging / register_secrets.
_KNOWN_SECRETS: set[str] = set()
_SECRET_SETTING = re.compile(r"(?i)key|secret|token|password")

# HTTP client libraries log every request URL at INFO; those URLs can carry credentials.
_QUIET_LOGGERS = ("httpx", "httpx2", "httpcore", "httpcore2")


def register_secrets(*values: str | None) -> None:
    """Hide these exact values in every log line (short values are ignored: too generic)."""
    _KNOWN_SECRETS.update(v for v in values if v and len(v) >= 8)


def register_settings_secrets(settings: Settings) -> None:
    """Every credential setting from .env (API keys, app ids, tokens, passwords). Settings that
    only point at a file (GMAIL_TOKEN_PATH...) are not secrets themselves."""
    for name, value in settings.model_dump().items():
        if not isinstance(value, str) or name.endswith(("_path", "_dir")):
            continue
        if _SECRET_SETTING.search(name) or name == "adzuna_app_id":
            register_secrets(value)


def redact(text: str) -> str:
    for secret in sorted(_KNOWN_SECRETS, key=len, reverse=True):
        text = text.replace(secret, _REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(settings: Settings) -> None:
    root = logging.getLogger()
    register_settings_secrets(settings)
    if getattr(root, "_pie_configured", False):
        return

    root.setLevel(settings.log_level.upper())
    console = logging.StreamHandler()
    console.setFormatter(RedactingFormatter(_FORMAT))
    root.addHandler(console)

    try:
        settings.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            settings.log_dir / "backend.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(RedactingFormatter(_FORMAT))
        root.addHandler(file_handler)
    except OSError:
        root.warning("File logging unavailable; continuing with console logging only")

    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    root._pie_configured = True  # type: ignore[attr-defined]
