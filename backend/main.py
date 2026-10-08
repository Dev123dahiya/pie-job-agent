"""FastAPI entrypoint. Start it with `python -m backend.serve --reload` (127.0.0.1 only)."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend import __version__
from backend.api.routes import (
    agent,
    applications,
    daily,
    dashboard,
    health,
    jobs,
    leads,
    logs,
    needs_input,
    resumes,
    review,
    runs,
)
from backend.api.routes import (
    settings as settings_routes,
)
from backend.core.config import Settings, get_settings
from backend.core.local_only import LocalOnlyMiddleware
from backend.core.logging import configure_logging
from backend.services.daily import DailyScheduler, build_daily_plan, parse_run_time, run_daily
from database.session import get_session_factory

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        scheduler = daily_scheduler(settings) if settings.daily_run_enabled else None
        if scheduler is not None:
            scheduler.start()
        else:
            logger.info("Daily discovery run is disabled (DAILY_RUN_ENABLED=false)")
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.stop()

    app = FastAPI(
        title="PIE Job Agent API",
        description="Personal Intelligence Engine - Job Agent backend",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Added last, so it runs first: non-local requests are refused before anything else.
    app.add_middleware(LocalOnlyMiddleware)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    app.include_router(health.router, prefix="/api")
    app.include_router(dashboard.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")
    app.include_router(agent.router, prefix="/api")
    app.include_router(resumes.router, prefix="/api")
    app.include_router(applications.router, prefix="/api")
    app.include_router(daily.router, prefix="/api")
    app.include_router(leads.router, prefix="/api")
    app.include_router(logs.router, prefix="/api")
    app.include_router(needs_input.router, prefix="/api")
    app.include_router(runs.router, prefix="/api")
    app.include_router(review.router, prefix="/api")
    app.include_router(settings_routes.router, prefix="/api")

    if settings.development_mode:
        logger.info("DEVELOPMENT_MODE is on: no real applications will be submitted")
    return app


def daily_scheduler(settings: Settings) -> DailyScheduler:
    """The in-process daily schedule: discovery + analysis (backend.services.daily), then
    auto-apply when DAILY_AUTO_APPLY is on (backend.services.daily_auto)."""
    factory = get_session_factory()

    def trigger() -> None:
        run_daily(factory, build_daily_plan(settings), "scheduled")
        if settings.daily_auto_apply:
            from agent.tools import build_default_registry
            from backend.core.profile import load_user_profile
            from backend.services.daily_auto import run_daily_auto
            from integrations.platforms.browser import get_platform_browser

            try:
                run_daily_auto(factory, settings, load_user_profile(), build_default_registry(),
                               lambda p: get_platform_browser(p, settings))  # fmt: skip
            except Exception:
                logger.exception("Daily auto-apply failed")

    return DailyScheduler(
        at=parse_run_time(settings.daily_run_time),
        tz=ZoneInfo(settings.timezone),
        factory=factory,
        trigger=trigger,
    )


app = create_app()
