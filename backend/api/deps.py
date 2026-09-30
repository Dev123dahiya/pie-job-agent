"""FastAPI dependency providers. Tests override these to inject fixtures and fakes."""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session, sessionmaker

from agent.analysis.analyzers import (
    AnalyzerUnavailableError,
    JobAnalyzer,
    resolve_analyzer,
    resolve_structured_claude,
)
from agent.claude_client import StructuredClaude
from agent.tools import ToolRegistry, build_default_registry
from backend.core.config import Settings, get_settings
from backend.core.profile import UserProfile, load_user_profile
from backend.services.agent_runner import AgentRunner
from backend.services.analysis import AnalysisContext
from backend.services.shortlist import ShortlistRules, load_shortlist_rules
from database.session import get_session_factory
from integrations.job_sources.base import JobSource
from integrations.job_sources.config import JobSourcesConfig, load_job_sources_config
from integrations.job_sources.registry import build_source

SourceBuilder = Callable[[str], JobSource]


def session_factory_dep() -> sessionmaker[Session]:
    """Factory for background work, which must not reuse the request-scoped session."""
    return get_session_factory()


def sources_config_dep(settings: Annotated[Settings, Depends(get_settings)]) -> JobSourcesConfig:
    return load_job_sources_config(settings.job_sources_path)


def profile_dep() -> UserProfile:
    return load_user_profile()


def rules_dep(settings: Annotated[Settings, Depends(get_settings)]) -> ShortlistRules:
    return load_shortlist_rules(settings.shortlist_rules_path)


def source_builder_dep(
    settings: Annotated[Settings, Depends(get_settings)],
    config: Annotated[JobSourcesConfig, Depends(sources_config_dep)],
) -> SourceBuilder:
    return lambda name: build_source(name, config, settings)


def analyzer_dep(
    settings: Annotated[Settings, Depends(get_settings)],
    config: Annotated[JobSourcesConfig, Depends(sources_config_dep)],
) -> JobAnalyzer:
    try:
        return resolve_analyzer(
            settings, config.search.location_patterns, config.search.term_aliases
        )
    except AnalyzerUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


_REGISTRY = build_default_registry()


def registry_dep() -> ToolRegistry:
    return _REGISTRY


def agent_claude_dep(settings: Annotated[Settings, Depends(get_settings)]) -> StructuredClaude:
    try:
        return resolve_structured_claude(settings)
    except AnalyzerUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def agent_runner_dep(
    settings: Annotated[Settings, Depends(get_settings)],
    config: Annotated[JobSourcesConfig, Depends(sources_config_dep)],
    rules: Annotated[ShortlistRules, Depends(rules_dep)],
    factory: Annotated[sessionmaker[Session], Depends(session_factory_dep)],
    claude: Annotated[StructuredClaude, Depends(agent_claude_dep)],
    registry: Annotated[ToolRegistry, Depends(registry_dep)],
) -> AgentRunner:
    def analysis_context(profile: UserProfile) -> AnalysisContext:
        analyzer = resolve_analyzer(
            settings, config.search.location_patterns, config.search.term_aliases
        )
        return AnalysisContext(
            analyzer=analyzer, profile=profile, rules=rules, replace_outdated=True
        )

    return AgentRunner(
        factory, claude, registry=registry, settings=settings,
        analysis_context_builder=analysis_context,
    )  # fmt: skip


def browser_dep(settings: Annotated[Settings, Depends(get_settings)]) -> Any:
    """The process-wide Playwright BrowserManager (tests inject their own)."""
    from integrations.browser import get_browser_manager

    return get_browser_manager(settings)
