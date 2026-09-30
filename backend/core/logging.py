"""Logging setup: console + rotating file under data/logs."""

import logging
import re
from logging.handlers import RotatingFileHandler

from backend.core.config import Settings

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

# Credential-bearing query parameters (e.g. Adzuna's app_id/app_key) are never written to logs.
_SECRET_PARAM = re.compile(
    r"(?i)([?&](?:app_id|app_key|api_key|apikey|key|token|access_token|secret|password)=)[^&\s\"']*"
)
# HTTP client libraries log every request URL at INFO; those URLs can carry credentials.
_QUIET_LOGGERS = ("httpx", "httpx2", "httpcore", "httpcore2")


def redact(text: str) -> str:
    return _SECRET_PARAM.sub(lambda m: m.group(1) + "[REDACTED]", text)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(settings: Settings) -> None:
    root = logging.getLogger()
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
