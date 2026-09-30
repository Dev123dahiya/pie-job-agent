"""The shipped board configuration (config/job_sources.yaml): every configured board reaches
its adapter, with no duplicates. No network: only plan() is called."""

from collections import Counter

import pytest

from backend.core.config import get_settings
from integrations.job_sources.config import JobSourcesConfig, load_job_sources_config
from integrations.job_sources.registry import build_source

# Boards added in Phase 5.6 (India-wide), each verified on the ATS's public API.
PHASE_5_6 = {
    "greenhouse": ["okta", "zenoti", "glance", "toast", "turing", "stripe", "sigmoid", "gitlab",
                   "newrelic", "bitwarden", "rubrik", "highradius"],
    "ashby": ["tekion", "thoughtworks", "spotdraft"],
    # SmartRecruiters identifiers are the API's own company identifiers (exact case).
    "smartrecruiters": ["Freshworks", "Experian", "SWIGGY", "Wise"],
}  # fmt: skip
EXPECTED_COUNTS = {"greenhouse": 28, "lever": 8, "ashby": 5, "smartrecruiters": 11}


@pytest.fixture
def config() -> JobSourcesConfig:
    return load_job_sources_config(get_settings().job_sources_path)


def planned(config: JobSourcesConfig, name: str) -> list[str]:
    source = build_source(name, config, get_settings())
    try:
        query = config.build_query([], [])
        return [str(next(iter(t.params.values()))) for t in source.plan(query)]
    finally:
        source.close()


@pytest.mark.parametrize("name", sorted(EXPECTED_COUNTS))
def test_every_configured_board_is_planned_once(config: JobSourcesConfig, name: str) -> None:
    boards = planned(config, name)
    assert len(boards) == EXPECTED_COUNTS[name]
    assert not [b for b, n in Counter(b.lower() for b in boards).items() if n > 1]


@pytest.mark.parametrize("name", sorted(PHASE_5_6))
def test_phase_5_6_boards_are_configured(config: JobSourcesConfig, name: str) -> None:
    boards = planned(config, name)
    assert set(PHASE_5_6[name]) <= set(boards)


def test_ats_sources_stay_enabled_and_adzuna_is_unchanged(config: JobSourcesConfig) -> None:
    for name in EXPECTED_COUNTS:
        source = config.source(name)
        assert source is not None and source.enabled
    adzuna = config.source("adzuna")
    assert adzuna is not None
    assert adzuna.options["created_is_publication"] is False
    assert adzuna.options["max_requests_per_run"] == 60 and adzuna.options["max_pages"] == 2
