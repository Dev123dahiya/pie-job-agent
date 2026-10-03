"""C9 Settings: validate, show the diff, back up, save. Runs on COPIES of the config files."""

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from backend.api.routes.settings import backup_dir_dep
from backend.core.config import REPO_ROOT, Settings, get_settings

FILES = {
    "job_sources.yaml": "job_sources.yaml",
    "user_profile.yaml": "user_profile.example.yaml",  # never the user's real profile
    "automation.yaml": "automation.yaml",
    "shortlist_rules.yaml": "shortlist_rules.yaml",
}


@pytest.fixture
def cfg(client: TestClient, tmp_path: Path) -> dict[str, Any]:
    folder = tmp_path / "config"
    folder.mkdir()
    for name, source in FILES.items():
        shutil.copy(REPO_ROOT / "config" / source, folder / name)
    settings = Settings(
        job_sources_path=folder / "job_sources.yaml",
        user_profile_path=folder / "user_profile.yaml",
        automation_path=folder / "automation.yaml",
        shortlist_rules_path=folder / "shortlist_rules.yaml",
    )
    backups = tmp_path / "backups"
    app = client.app  # type: ignore[attr-defined]
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[backup_dir_dep] = lambda: backups
    return {"folder": folder, "backups": backups}


def load(cfg: dict[str, Any], name: str) -> Any:
    return yaml.safe_load((cfg["folder"] / name).read_text(encoding="utf-8"))


def test_read_current_settings(client: TestClient, cfg: dict[str, Any]) -> None:
    body = client.get("/api/settings").json()
    assert body["limits"] == {"total": 20, "naukri": 15, "linkedin": 5, "company_site": 10}
    assert body["min_match_score"] == 70 and body["keywords"]
    assert "current_ctc" in body["answer_questions"]
    assert "E1" in body["read_only"]["models"]["note"]


def test_preview_shows_a_diff_and_writes_nothing(client: TestClient, cfg: dict[str, Any]) -> None:
    before = (cfg["folder"] / "automation.yaml").read_text(encoding="utf-8")
    limits = {"total": 18, "naukri": 12, "linkedin": 4, "company_site": 8}
    res = client.post("/api/settings/preview", json={"limits": limits})
    assert res.status_code == 200
    [f] = res.json()["files"]
    assert f["name"] == "automation.yaml" and "-  naukri: 15" in f["diff"]
    assert "+  naukri: 12" in f["diff"]
    assert (cfg["folder"] / "automation.yaml").read_text(encoding="utf-8") == before
    assert not cfg["backups"].exists()


def test_save_backs_up_keeps_comments_and_syncs_search_and_profile(
    client: TestClient, cfg: dict[str, Any]
) -> None:
    sources_before = (cfg["folder"] / "job_sources.yaml").read_text(encoding="utf-8")
    res = client.post(
        "/api/settings",
        json={
            "keywords": ["Python Developer", " Backend  Developer ", "python developer"],
            "locations": ["Gurugram", "Remote"],
            "excluded_companies": ["Acme Staffing"],
            "freshness_hours": 72,
            "min_match_score": 75,
            "answers": {"notice_period": "Immediate", "current_ctc": "0 (fresher)"},
        },
    )
    assert res.status_code == 200, res.text
    assert set(res.json()["saved"]) == {
        "job_sources.yaml",
        "user_profile.yaml",
        "shortlist_rules.yaml",
    }
    sources, profile = load(cfg, "job_sources.yaml"), load(cfg, "user_profile.yaml")
    keywords = ["Python Developer", "Backend Developer"]  # cleaned and de-duplicated
    assert sources["search"]["terms"] == keywords == profile["preferences"]["preferred_roles"]
    assert sources["search"]["locations"] == ["Gurugram", "Remote"]
    assert profile["preferences"]["preferred_locations"] == ["Gurugram", "Remote"]
    assert profile["preferences"]["excluded_companies"] == ["Acme Staffing"]
    assert profile["preferences"]["maximum_application_age_hours"] == 72
    answers = profile["verified_profile"]["verified_answers"]
    assert answers["notice_period"] == "Immediate" and answers["current_ctc"] == "0 (fresher)"
    assert load(cfg, "shortlist_rules.yaml")["min_match_score"] == 75
    # The sources and their comments are untouched; only the edited keys changed.
    after = (cfg["folder"] / "job_sources.yaml").read_text(encoding="utf-8")
    assert after.count("#") >= sources_before.count("#") - 3
    assert load(cfg, "job_sources.yaml")["sources"] == yaml.safe_load(sources_before)["sources"]
    # One backup per changed file, holding the old text.
    backups = sorted(p.name for p in cfg["backups"].iterdir())
    assert len(backups) == 3 and any(b.startswith("job_sources.") for b in backups)
    old = next(p for p in cfg["backups"].iterdir() if p.name.startswith("job_sources."))
    assert old.read_text(encoding="utf-8") == sources_before


@pytest.mark.parametrize(
    "change",
    [
        {"limits": {"total": 20, "naukri": 5, "linkedin": 5, "company_site": 10}},  # LI >= Naukri
        {"limits": {"total": -1, "naukri": 15, "linkedin": 5, "company_site": 10}},
        {"min_match_score": 101},
        {"freshness_hours": 0},
        {"keywords": []},
        {"keywords": ["   "]},
        {"answers": {"password": "x"}},
        {"answers": {"notice_period": "  "}},
        {"surprise": True},
        {},
    ],
)
def test_invalid_changes_save_nothing(
    client: TestClient, cfg: dict[str, Any], change: dict[str, Any]
) -> None:
    before = {p.name: p.read_text(encoding="utf-8") for p in cfg["folder"].iterdir()}
    res = client.post("/api/settings", json=change)
    assert res.status_code == 422
    after = {p.name: p.read_text(encoding="utf-8") for p in cfg["folder"].iterdir()}
    assert after == before and not cfg["backups"].exists()
