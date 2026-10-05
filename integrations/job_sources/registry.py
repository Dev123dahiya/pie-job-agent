"""Builds JobSource adapters from config. New adapters register a factory here."""

from collections.abc import Callable
from typing import Any

from backend.core.config import Settings
from integrations.job_sources.adzuna import AdzunaSource
from integrations.job_sources.ashby import AshbySource
from integrations.job_sources.base import JobSource
from integrations.job_sources.config import JobSourcesConfig, SourceConfig
from integrations.job_sources.greenhouse import GreenhouseSource
from integrations.job_sources.lever import LeverSource
from integrations.job_sources.mock import MockSource
from integrations.job_sources.remotive import RemotiveSource
from integrations.job_sources.smartrecruiters import SmartRecruitersSource

Factory = Callable[[SourceConfig, JobSourcesConfig, Settings], JobSource]


def _greenhouse(cfg: SourceConfig, all_cfg: JobSourcesConfig, settings: Settings) -> JobSource:
    opts = cfg.options
    return GreenhouseSource(
        boards=list(opts.get("boards") or []),
        skills_vocabulary=all_cfg.skills_vocabulary,
        user_agent=settings.http_user_agent,
        request_delay_seconds=float(opts.get("request_delay_seconds", 1.0)),
        fetch_pay_ranges=bool(opts.get("fetch_pay_ranges", True)),
    )


def _platform(kind: str) -> "Factory":
    """Naukri / LinkedIn / Instahyre: searched in the platform's own visible browser."""

    def build(cfg: SourceConfig, all_cfg: JobSourcesConfig, settings: Settings) -> JobSource:
        from backend.core.config import REPO_ROOT
        from backend.core.profile import load_user_profile
        from integrations.job_sources.platforms import (
            FounditSource,
            InstahyreSource,
            LinkedInSource,
            NaukriSource,
        )
        from integrations.platforms.browser import get_platform_browser

        def years() -> float | None:
            facts = load_user_profile(settings.user_profile_path).verified_profile
            set_by_user = "total_experience_years" in facts.model_fields_set
            return facts.total_experience_years if set_by_user else None

        common: dict[str, Any] = {}
        cls: Any
        if kind == "foundit":
            cls = FounditSource
        elif kind == "instahyre":
            cls = InstahyreSource
            if cfg.options.get("search_experience_years") is not None:
                common["search_years"] = int(cfg.options["search_experience_years"])
        else:
            cls = NaukriSource if kind == "naukri" else LinkedInSource
        return cls(
            **common,
            browser=get_platform_browser(kind, settings),
            skills_vocabulary=all_cfg.skills_vocabulary,
            data_dir=REPO_ROOT / "data",
            max_searches=int(cfg.options.get("max_searches", 4)),
            pages=int(cfg.options.get("pages", 1)),
            max_details=int(cfg.options.get("max_details", 15)),
            request_delay_seconds=_delay(cfg, 8.0),
            experience_years=years,
        )

    return build


def _mock(cfg: SourceConfig, all_cfg: JobSourcesConfig, settings: Settings) -> JobSource:
    return MockSource(skills_vocabulary=all_cfg.skills_vocabulary)


def _delay(cfg: SourceConfig, default: float = 1.0) -> float:
    return float(cfg.options.get("request_delay_seconds", default))


def _lever(cfg: SourceConfig, all_cfg: JobSourcesConfig, settings: Settings) -> JobSource:
    return LeverSource(
        list(cfg.options.get("companies") or []),
        skills_vocabulary=all_cfg.skills_vocabulary,
        user_agent=settings.http_user_agent,
        request_delay_seconds=_delay(cfg),
    )


def _ashby(cfg: SourceConfig, all_cfg: JobSourcesConfig, settings: Settings) -> JobSource:
    return AshbySource(
        list(cfg.options.get("boards") or []),
        skills_vocabulary=all_cfg.skills_vocabulary,
        user_agent=settings.http_user_agent,
        request_delay_seconds=_delay(cfg),
    )


def _smartrecruiters(cfg: SourceConfig, all_cfg: JobSourcesConfig, settings: Settings) -> JobSource:
    return SmartRecruitersSource(
        list(cfg.options.get("companies") or []),
        skills_vocabulary=all_cfg.skills_vocabulary,
        user_agent=settings.http_user_agent,
        country=str(cfg.options.get("country", "in")),
        request_delay_seconds=_delay(cfg),
    )


def _remotive(cfg: SourceConfig, all_cfg: JobSourcesConfig, settings: Settings) -> JobSource:
    return RemotiveSource(
        list(cfg.options.get("categories") or []),
        skills_vocabulary=all_cfg.skills_vocabulary,
        user_agent=settings.http_user_agent,
        request_delay_seconds=_delay(cfg, 2.0),
        max_requests=int(cfg.options.get("max_requests_per_run", 3)),
    )


def _adzuna(cfg: SourceConfig, all_cfg: JobSourcesConfig, settings: Settings) -> JobSource:
    return AdzunaSource(
        app_id=settings.adzuna_app_id,
        app_key=settings.adzuna_app_key,
        skills_vocabulary=all_cfg.skills_vocabulary,
        user_agent=settings.http_user_agent,
        country=str(cfg.options.get("country", "in")),
        max_days_old=int(cfg.options.get("max_days_old", 2)),
        max_pages=int(cfg.options.get("max_pages", 2)),
        results_per_page=int(cfg.options.get("results_per_page", 50)),
        request_delay_seconds=_delay(cfg, 3.0),
        max_requests=int(cfg.options.get("max_requests_per_run", 60)),
        location_queries={
            str(k): str(v) for k, v in (cfg.options.get("location_queries") or {}).items()
        },
        created_is_publication=bool(cfg.options.get("created_is_publication", False)),
    )


FACTORIES: dict[str, Factory] = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "smartrecruiters": _smartrecruiters,
    "remotive": _remotive,
    "adzuna": _adzuna,
    "mock": _mock,
    "naukri": _platform("naukri"),
    "linkedin": _platform("linkedin"),
    "instahyre": _platform("instahyre"),
    "foundit": _platform("foundit"),
}


class UnknownSourceError(ValueError):
    pass


def build_source(name: str, config: JobSourcesConfig, settings: Settings) -> JobSource:
    """Build a configured source by name (disabled sources can still be run explicitly)."""
    cfg = config.source(name)
    if cfg is None:
        raise UnknownSourceError(f"No source named {name!r} in job_sources.yaml")
    factory = FACTORIES.get(cfg.type)
    if factory is None:
        raise UnknownSourceError(f"Unknown source type {cfg.type!r} for source {name!r}")
    return factory(cfg, config, settings)


# Where each ATS adapter reads its board list from (config key, entry shape).
BOARD_KEYS = {
    "greenhouse": "boards",  # plain board tokens
    "lever": "companies",  # {slug, name}
    "ashby": "boards",  # {slug, name}
    "smartrecruiters": "companies",  # {slug, name} - slug is the exact company identifier
}


def build_source_for_boards(
    platform: str,
    boards: list[tuple[str, str]],
    config: JobSourcesConfig,
    settings: Settings,
) -> JobSource:
    """The platform's EXISTING adapter, limited to `boards` ((board id, company name) pairs).

    Everything else (delays, country filter, pay-range fetching...) comes from the configured
    source of that type, so boards found through leads are crawled exactly like configured
    ones and produce the same source name and source_job_id format."""
    key = BOARD_KEYS.get(platform)
    cfg = next((s for s in config.sources if s.type == platform), None)
    if key is None or cfg is None:
        raise UnknownSourceError(f"No configured {platform!r} source to crawl boards with")
    entries: list[object] = (
        [board for board, _ in boards]
        if platform == "greenhouse"
        else [{"slug": board, "name": name} for board, name in boards]
    )
    limited = SourceConfig.model_validate({**cfg.model_dump(), key: entries})
    return FACTORIES[platform](limited, config, settings)
