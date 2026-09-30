"""Application settings, loaded from environment variables / `.env`.

Secrets never live in source code; see `.env.example` for the full list.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "PIE Job Agent"
    environment: str = "local"

    # When true: never submit real applications; use mock jobs/forms/emails.
    development_mode: bool = True

    # SQLite file under data/ (git-ignored). PostgreSQL also works: set DATABASE_URL, e.g.
    # postgresql+psycopg://pie:pie@localhost:5432/pie_job_agent (pip install -e ".[postgres]").
    database_url: str = f"sqlite+pysqlite:///{(REPO_ROOT / 'data' / 'pie.db').as_posix()}"
    database_echo: bool = False

    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )

    # IANA timezone used for "today" boundaries on the dashboard.
    timezone: str = "Asia/Kolkata"

    log_level: str = "INFO"
    log_dir: Path = REPO_ROOT / "data" / "logs"
    export_dir: Path = REPO_ROOT / "data" / "exports"
    resumes_dir: Path = REPO_ROOT / "resumes"
    user_profile_path: Path = REPO_ROOT / "config" / "user_profile.yaml"
    # Master resume (read-only source of truth). Unset: the first file in resumes/master/.
    master_resume_path: Path | None = None

    job_sources_path: Path = REPO_ROOT / "config" / "job_sources.yaml"
    shortlist_rules_path: Path = REPO_ROOT / "config" / "shortlist_rules.yaml"
    # Identifies this tool honestly to job-board APIs.
    http_user_agent: str = "PIE-Job-Agent/0.1 (personal job search; low volume)"

    # Claude job analysis.
    # auto       -> claude_api if ANTHROPIC_API_KEY is set, else claude_cli if `claude` is on PATH
    # claude_api -> Anthropic API (ANTHROPIC_API_KEY)
    # claude_cli -> local Claude Code CLI (uses your Claude subscription login)
    # heuristic  -> deterministic keyword matcher; for tests/offline only, labelled as not-Claude
    analyzer_backend: str = "auto"
    anthropic_api_key: str | None = None
    claude_model: str = "claude-opus-5"
    claude_effort: str = "medium"
    claude_cli_path: str = "claude"
    # Model for the CLI backend; empty = the CLI's own default model.
    claude_cli_model: str | None = None
    claude_timeout_seconds: int = 240

    # --- Browser execution of applications (Phase 5) ---
    # LIVE external submission. Off by default and never switched on by code. It only takes
    # effect with DEVELOPMENT_MODE=false (see live_execution_block) and every submission also
    # needs the user's explicit per-application confirmation and a visible browser.
    external_live_enabled: bool = False
    # auto -> Playwright's bundled Chromium, else installed Microsoft Edge, else Google Chrome.
    browser_channel: str = "auto"
    # Must be false for any session that may need you (CAPTCHA, OTP, login...) and for LIVE.
    browser_headless: bool = True
    browser_timeout_ms: int = 30_000
    # An existing, user-configured browser profile to reuse a login you made yourself. PIE never
    # reads its cookies and never stores passwords. Unset: every session starts logged out.
    browser_user_data_dir: Path | None = None
    # Screenshots and other execution artifacts.
    application_artifacts_dir: Path = REPO_ROOT / "data" / "applications"

    # --- Daily discovery + analysis (Phase 5.5) ---
    # Runs inside the API process (uvicorn backend.main:app) once a day at DAILY_RUN_TIME in
    # TIMEZONE; if the machine was off at that time, it runs on the next start that day.
    # Discovery and analysis only: it never creates or submits applications.
    daily_run_enabled: bool = True
    daily_run_time: str = "07:00"  # 24h HH:MM
    # Most fresh jobs one daily run sends to Claude (each analysis is one Claude call).
    daily_run_analysis_limit: int = 50

    # Adzuna Jobs API (optional job source): your own free key from developer.adzuna.com.
    adzuna_app_id: str | None = None
    adzuna_app_key: str | None = None

    # Gmail OAuth (used from Phase 6 onward).
    gmail_credentials_path: Path | None = None
    gmail_token_path: Path | None = None


def live_execution_block(settings: "Settings") -> tuple[str, str] | None:
    """Why LIVE external actions are not allowed, or None if they are.

    DEVELOPMENT_MODE=true always wins: EXTERNAL_LIVE_ENABLED is then treated as false, whatever
    its configured value. Only DEVELOPMENT_MODE=false + EXTERNAL_LIVE_ENABLED=true allows live.
    """
    if settings.development_mode:
        return (
            "DEVELOPMENT_MODE_BLOCKED",
            "DEVELOPMENT_MODE is true, so live external actions are blocked regardless of "
            "EXTERNAL_LIVE_ENABLED. Nothing was done.",
        )
    if not settings.external_live_enabled:
        return ("LIVE_NOT_ENABLED", "Live external actions are disabled (EXTERNAL_LIVE_ENABLED).")
    return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
