from datetime import UTC, datetime
from typing import Any

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


# --- D1: Home page --------------------------------------------------------------------------


def test_every_pipeline_state_has_one_of_the_six_statuses() -> None:
    from backend.services.statuses import USER_STATUSES, user_status
    from database.enums import JobPipelineState

    assert {user_status(s) for s in JobPipelineState} <= set(USER_STATUSES)


def test_home_counts_six_statuses_and_never_claims_live(db: Session) -> None:
    from backend.core.config import Settings
    from backend.services.dashboard import build_home

    now = datetime(2026, 9, 28, 14, 30, tzinfo=UTC)
    seed_mock_data(db, now=now)
    settings = Settings(development_mode=True, external_live_enabled=True)
    home = build_home(db, settings, now=now)
    total = sum(home.all_time.model_dump().values())
    assert total == 10  # every mock job is counted exactly once
    assert home.all_time.applied >= 1 and home.all_time.manual == 0
    assert home.quick_links.needs_input == home.all_time.needs_input
    assert home.quick_links.manual == 0 and home.quick_links.waiting_for_review == 0
    # DEVELOPMENT_MODE wins over the live flag: no live banner.
    assert home.live_possible is False and "DEVELOPMENT_MODE" in home.live_note
    assert [p.platform for p in home.platforms] == [
        "naukri", "linkedin", "instahyre", "company_site",
    ]  # fmt: skip
    assert all(p.mode == "dry-run" for p in home.platforms)
    live = build_home(db, Settings(development_mode=False, external_live_enabled=True), now=now)
    assert live.live_possible is True


def test_home_endpoint(client: TestClient) -> None:
    body = client.get("/api/dashboard/home").json()
    assert set(body) >= {"live_possible", "platforms", "today", "all_time", "quick_links"}
    assert body["all_time"] == dict.fromkeys(
        ["found", "skipped", "applied", "needs_input", "manual", "failed"], 0
    )


def test_home_shows_the_saved_naukri_login(db: Session, tmp_path: Any) -> None:
    from backend.core.config import Settings
    from backend.services.dashboard import build_home
    from integrations.platforms.base import LoginCheck, save_status

    home = build_home(db, Settings(), data_dir=tmp_path)
    naukri = next(p for p in home.platforms if p.platform == "naukri")
    assert (
        naukri.login == "never" and naukri.login_command and "login naukri" in naukri.login_command
    )
    save_status(tmp_path, "naukri", LoginCheck("blocked", "Naukri refused PIE's browser"))
    home = build_home(db, Settings(), data_dir=tmp_path)
    assert next(p for p in home.platforms if p.platform == "naukri").login == "blocked"


def test_home_shows_both_platform_logins(db: Session, tmp_path: Any) -> None:
    from backend.core.config import Settings
    from backend.services.dashboard import build_home

    home = build_home(db, Settings(), data_dir=tmp_path)
    linkedin = next(p for p in home.platforms if p.platform == "linkedin")
    assert linkedin.login == "never" and "login linkedin" in (linkedin.login_command or "")
