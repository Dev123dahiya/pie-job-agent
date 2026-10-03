"""B7: daily limits config (REQUIREMENTS 10)."""

from pathlib import Path

import pytest

from backend.core.config import REPO_ROOT
from backend.services.limits import (
    LimitsConfigError,
    load_automation,
    platform_of,
)
from database.models import Job


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "automation.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_shipped_config_has_the_agreed_limits() -> None:
    limits = load_automation(REPO_ROOT / "config" / "automation.yaml").daily_limits
    assert (limits.total, limits.naukri, limits.linkedin, limits.company_site) == (20, 15, 5, 10)


def test_missing_file_means_the_agreed_defaults(tmp_path: Path) -> None:
    limits = load_automation(tmp_path / "nope.yaml").daily_limits
    assert (limits.total, limits.naukri, limits.linkedin, limits.company_site) == (20, 15, 5, 10)


@pytest.mark.parametrize(
    "text",
    [
        "daily_limits: {naukri: 5, linkedin: 5}",  # LinkedIn must be lower than Naukri
        "daily_limits: {total: -1}",
        "daily_limits: {total: many}",
        "daily_limits: {indeed: 3}",  # unknown platform: a typo must not be ignored
        "daily_limits: [1, 2",  # broken YAML
    ],
)
def test_invalid_config_is_an_error_not_a_silent_default(tmp_path: Path, text: str) -> None:
    with pytest.raises(LimitsConfigError):
        load_automation(write(tmp_path, text))


def test_linkedin_can_be_switched_off(tmp_path: Path) -> None:
    limits = load_automation(write(tmp_path, "daily_limits: {linkedin: 0}")).daily_limits
    assert limits.linkedin == 0


@pytest.mark.parametrize(
    ("source", "platform"),
    [
        ("naukri", "naukri"),
        ("LinkedIn", "linkedin"),
        ("greenhouse", "company_site"),
        ("lever", "company_site"),
        ("mock", "company_site"),
    ],
)
def test_platform_of_a_job(source: str, platform: str) -> None:
    assert platform_of(Job(source=source)) == platform
