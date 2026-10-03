"""D5: the Logs page API reads data/logs, filters it and never shows a secret."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.core.config import Settings, get_settings

RUN = "2b9d9c4a-6990-4d70-9676-5967b4214041"
OLD = (
    "2026-09-30 09:00:00,001 INFO     pie.agent: run=00000000-aaaa job=- action=STARTED\n"
    "2026-09-30 09:00:01,002 DEBUG    httpx: GET https://api.example/?app_key=leakedkey123\n"
)
CURRENT = (
    f"2026-10-02 10:00:00,000 INFO     pie.agent: run={RUN} action=JOB_DISCOVERED platform=naukri\n"
    "2026-10-02 10:00:05,000 WARNING  integrations.job_sources: greenhouse board slow\n"
    "2026-10-02 10:00:06,000 ERROR    backend.main: Unhandled error on GET /api/jobs\n"
    "Traceback (most recent call last):\n"
    '  File "x.py", line 1, in <module>\n'
    "ValueError: token=abc123secretvalue\n"
    "2026-10-03 08:00:00,000 INFO     backend.main: LinkedIn check password=hunter2hunter2\n"
)


@pytest.fixture
def logs(client: TestClient, tmp_path: Path) -> Any:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "backend.log.1").write_text(OLD, encoding="utf-8")
    (log_dir / "backend.log").write_text(CURRENT, encoding="utf-8")
    settings = Settings(log_dir=log_dir)
    client.app.dependency_overrides[get_settings] = lambda: settings  # type: ignore[attr-defined]
    return client


def get(client: TestClient, **params: Any) -> dict[str, Any]:
    res = client.get("/api/logs", params=params)
    assert res.status_code == 200, res.text
    return res.json()


def test_all_lines_newest_first_with_tracebacks_attached(logs: TestClient) -> None:
    body = get(logs)
    assert body["total"] == 6
    assert body["items"][0]["message"].startswith("LinkedIn check")
    error = next(i for i in body["items"] if i["level"] == "ERROR")
    assert "Traceback" in error["message"] and "ValueError" in error["message"]


def test_secrets_are_never_shown_or_downloaded(logs: TestClient) -> None:
    text = str(get(logs)) + logs.get("/api/logs/download").text
    for secret in ("leakedkey123", "abc123secretvalue", "hunter2hunter2"):
        assert secret not in text
    assert "[REDACTED]" in text


def test_filters(logs: TestClient) -> None:
    assert get(logs, level="WARNING")["total"] == 2  # WARNING and ERROR
    assert get(logs, level="ERROR")["total"] == 1
    assert get(logs, run=RUN)["total"] == 1
    assert get(logs, platform="naukri")["total"] == 1
    assert get(logs, platform="linkedin")["total"] == 1
    assert get(logs, platform="company_site")["total"] == 1  # "greenhouse"
    assert get(logs, date_from="2026-10-02", date_to="2026-10-02")["total"] == 3
    assert get(logs, date_to="2026-09-30")["total"] == 2
    assert get(logs, q="BOARD SLOW")["total"] == 1
    page = get(logs, limit=2, offset=2)
    assert page["total"] == 6 and len(page["items"]) == 2


def test_invalid_filters_are_refused(logs: TestClient) -> None:
    assert logs.get("/api/logs", params={"level": "LOUD"}).status_code == 422
    assert logs.get("/api/logs", params={"platform": "indeed"}).status_code == 422
    assert logs.get("/api/logs", params={"run": "x; rm -rf"}).status_code == 422
    bad_dates = {"date_from": "2026-10-03", "date_to": "2026-10-01"}
    assert logs.get("/api/logs", params=bad_dates).status_code == 422


def test_download_is_the_filtered_log(logs: TestClient) -> None:
    res = logs.get("/api/logs/download", params={"level": "WARNING"})
    assert res.status_code == 200
    assert "attachment" in res.headers["content-disposition"]
    lines = [ln for ln in res.text.splitlines() if ln[:4].isdigit()]
    assert len(lines) == 2 and "WARNING" in lines[0] and "ERROR" in lines[1]  # oldest first


def test_no_log_files_is_an_empty_list(client: TestClient, tmp_path: Path) -> None:
    settings = Settings(log_dir=tmp_path / "none")
    client.app.dependency_overrides[get_settings] = lambda: settings  # type: ignore[attr-defined]
    assert get(client) == {"items": [], "total": 0, "limit": 200, "offset": 0}
