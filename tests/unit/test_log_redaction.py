"""Credentials in request URLs never reach the logs."""

import logging

from backend.core.logging import RedactingFormatter, redact

URL = "GET https://api.adzuna.com/v1/api/jobs/in/search/1?app_id=a1b2c3d4&app_key=0123456789abcdef&what=SDE"


def test_redact_hides_credential_params() -> None:
    out = redact(URL)
    assert "a1b2c3d4" not in out and "0123456789abcdef" not in out
    assert "app_id=[REDACTED]&app_key=[REDACTED]&what=SDE" in out


def test_formatter_redacts_messages_and_args() -> None:
    record = logging.LogRecord(
        "httpx2", logging.INFO, __file__, 1, "HTTP Request: %s", (URL,), None
    )
    out = RedactingFormatter("%(message)s").format(record)
    assert "0123456789abcdef" not in out and "[REDACTED]" in out


def test_http_client_request_logs_are_quiet() -> None:
    from backend.core.config import Settings
    from backend.core.logging import configure_logging

    configure_logging(Settings())
    assert logging.getLogger("httpx2").getEffectiveLevel() >= logging.WARNING
