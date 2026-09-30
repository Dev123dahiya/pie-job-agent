from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.core.config import REPO_ROOT
from backend.core.profile import load_user_profile

EXAMPLE = REPO_ROOT / "config" / "user_profile.example.yaml"


def test_example_profile_loads() -> None:
    profile = load_user_profile(EXAMPLE)
    prefs = profile.preferences
    assert "Gurugram" in prefs.preferred_locations
    assert "Software Engineer" in prefs.preferred_roles
    assert prefs.minimum_salary == 1_000_000
    assert prefs.maximum_application_age_hours == 48


def test_invalid_profile_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "profile.yaml"
    bad.write_text("name: X\nemail: not-an-email\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_user_profile(bad)


def test_minimal_profile_uses_defaults(tmp_path: Path) -> None:
    p = tmp_path / "profile.yaml"
    p.write_text("name: X\nemail: x@example.com\n", encoding="utf-8")
    profile = load_user_profile(p)
    assert profile.preferences.minimum_salary is None
    assert profile.verified_profile.verified_answers == {}


def test_example_profile_separates_facts_from_preferences() -> None:
    profile = load_user_profile(EXAMPLE)
    facts = profile.verified_profile.model_dump()
    assert "preferred_roles" not in facts and "minimum_salary" not in facts
    assert "skills" not in profile.preferences.model_dump()
    assert profile.name == "Your Name"


def test_legacy_flat_layout_still_loads(tmp_path: Path) -> None:
    p = tmp_path / "profile.yaml"
    p.write_text(
        "name: X\nemail: x@example.com\nskills: [Python]\npreferences:\n  preferred_roles: [SDE]\n",
        encoding="utf-8",
    )
    profile = load_user_profile(p)
    assert profile.verified_profile.skills == ["Python"]
    assert profile.preferences.preferred_roles == ["SDE"]


def test_all_skills_includes_project_technologies(tmp_path: Path) -> None:
    p = tmp_path / "profile.yaml"
    p.write_text(
        "verified_profile:\n  name: X\n  email: x@example.com\n  skills: [Python, SQL]\n"
        "  projects:\n    - {name: A, description: B, technologies: [FastAPI, python]}\n",
        encoding="utf-8",
    )
    assert load_user_profile(p).verified_profile.all_skills() == ["Python", "SQL", "FastAPI"]


def test_unconfirmed_skills_are_never_verified(tmp_path: Path) -> None:
    p = tmp_path / "profile.yaml"
    p.write_text(
        "verified_profile:\n  name: X\n  email: x@example.com\n  skills: [Python, Go]\n"
        "  unconfirmed_skills: [go]\n"
        "  projects:\n    - {name: A, description: B, technologies: [Go, FastAPI]}\n",
        encoding="utf-8",
    )
    facts = load_user_profile(p).verified_profile
    assert facts.confirmed_skills() == ["Python"]
    assert facts.all_skills() == ["Python", "FastAPI"]  # not even via project technologies


def test_real_profile_marks_go_unresolved_if_present() -> None:
    from backend.core.config import get_settings

    path = get_settings().user_profile_path
    if not path.exists():
        pytest.skip("no personal profile on this machine")
    facts = load_user_profile(path).verified_profile
    if any(s.lower() == "go" for s in facts.skills):
        assert "go" not in {s.lower() for s in facts.all_skills()}
