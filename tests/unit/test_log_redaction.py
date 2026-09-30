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


# --- A4: headers, keys, passwords, OTPs, tokens and the real .env values -------------------

import pytest  # noqa: E402

from backend.core import logging as pie_logging  # noqa: E402

FAKE_ANTHROPIC = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


@pytest.mark.parametrize(
    ("line", "secret"),
    [
        (f"calling Claude with key {FAKE_ANTHROPIC}", FAKE_ANTHROPIC[7:]),
        (
            "headers={'Authorization': 'Bearer eyJhbGciOiJIUzI1NiJ9.abc.def'}",
            "eyJhbGciOiJIUzI1NiJ9",
        ),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
        ("x-api-key: abcdef1234567890abcdef", "abcdef1234567890abcdef"),
        ("Cookie: li_at=AQEDAR123secret; JSESSIONID=ajax:123", "AQEDAR123secret"),
        ("Set-Cookie: nauk_at=xyz987secret; Path=/; HttpOnly", "xyz987secret"),
        ('{"password": "hunter2hunter2"}', "hunter2hunter2"),
        ("login failed for user=me password=Tr0ub4dor&3", "Tr0ub4dor"),
        ("entered otp=482913 in the form", "482913"),
        ("OTP: 771204", "771204"),
        ("refresh_token=1//0gAbCdEf", "1//0gAbCdEf"),
        ('"client_secret": "GOCSPX-abcdefghijkl"', "GOCSPX-abcdefghijkl"),
        ("postgresql+psycopg://pie:s3cretpass@localhost:5432/pie", "s3cretpass"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdefghijklmnop"),
    ],
)
def test_secrets_are_hidden(line: str, secret: str) -> None:
    out = redact(line)
    assert secret not in out and "[REDACTED]" in out, out


@pytest.mark.parametrize(
    "line",
    [
        "Analyzed 12 jobs with max_tokens=16000 in 3.2s",
        "challenge detected: OTP required, pausing for the user",
        "job=Software Engineer company=Acme Labs location=Gurugram",
        "Refused a non-local request (client=192.168.1.5, host=evil.example)",
        "The API is served at http://127.0.0.1:8000/api/health",
    ],
)
def test_ordinary_log_lines_are_unchanged(line: str) -> None:
    assert redact(line) == line


def test_real_setting_values_are_hidden_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.core.config import Settings

    monkeypatch.setattr(pie_logging, "_KNOWN_SECRETS", set())
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        anthropic_api_key="plain-looking-value-123456",
        adzuna_app_id="a0a0a0a0",
        adzuna_app_key="f00ba4f00ba4f00ba4f00ba4",
        gmail_token_path="data/gmail/token.json",
    )
    pie_logging.register_settings_secrets(settings)
    line = "retrying plain-looking-value-123456 / a0a0a0a0 / f00ba4f00ba4f00ba4f00ba4 in data"
    out = redact(line)
    for value in ("plain-looking-value-123456", "a0a0a0a0", "f00ba4f00ba4f00ba4f00ba4"):
        assert value not in out
    assert redact("token file at data/gmail/token.json") == "token file at data/gmail/token.json"


def test_exception_tracebacks_are_redacted() -> None:
    try:
        raise RuntimeError(f"auth failed with {FAKE_ANTHROPIC}")
    except RuntimeError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", (), sys.exc_info())
    out = RedactingFormatter("%(message)s").format(record)
    assert "Traceback" in out and FAKE_ANTHROPIC[7:] not in out
