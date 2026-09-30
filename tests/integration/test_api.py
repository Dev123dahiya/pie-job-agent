from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.services.dashboard import build_overview
from backend.services.mock_data import seed_mock_data


def test_health_ok(client: TestClient) -> None:
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["development_mode"] is True
    assert body["components"]["database"]["status"] == "ok"
    assert body["components"]["claude"]["status"] == "not_configured"


def test_overview_empty_database(client: TestClient) -> None:
    res = client.get("/api/dashboard/overview")
    assert res.status_code == 200
    body = res.json()
    assert body["jobs_discovered_today"] == 0
    assert body["agent"]["status"] == "IDLE"
    assert [s["stage"] for s in body["funnel"]] == [
        "Discovered",
        "Shortlisted",
        "Applied",
        "Assessment",
        "Interview",
        "Offer",
    ]


def test_overview_counts_from_mock_data(db: Session) -> None:
    now = datetime(2026, 9, 28, 14, 30, tzinfo=UTC)  # 20:00 IST
    seed_mock_data(db, now=now)
    ov = build_overview(db, now=now, tz="Asia/Kolkata")

    funnel = {s.stage: s.count for s in ov.funnel}
    assert funnel == {
        "Discovered": 10,
        "Shortlisted": 7,
        "Applied": 4,
        "Assessment": 2,
        "Interview": 1,
        "Offer": 0,
    }
    assert ov.jobs_discovered_today == 10
    assert ov.applications_submitted == 4  # the OTP-blocked one is NOT counted as applied
    assert ov.rejections == 1
    assert ov.recruiter_responses == 1
    assert ov.agent.status == "COMPLETED"
    assert ov.agent.error_count == 1
    assert len(ov.recent_activity) == 10


def test_unknown_route_404(client: TestClient) -> None:
    assert client.get("/api/nope").status_code == 404


def test_today_uses_user_timezone(db: Session) -> None:
    # Jobs discovered 08:00-17:00 UTC on Sep 28. At 20:00 UTC it is already Sep 29 01:30 in
    # India, so none of them were discovered "today" there - but all were in UTC.
    now = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)
    seed_mock_data(db, now=now)
    assert build_overview(db, now=now, tz="Asia/Kolkata").jobs_discovered_today == 0
    assert build_overview(db, now=now, tz="UTC").jobs_discovered_today == 10
