"""Engine and session factory.

SQLite (the default, a single file under data/) is set up to behave like PostgreSQL where it
matters, and to cope with PIE's two writers (the main work and the daily-run heartbeat):

- foreign keys are enforced;
- WAL journal: the dashboard can read while a run writes;
- a busy timeout: a writer waits for the other one instead of failing at once;
- nested transactions (SAVEPOINTs) always sit inside a real transaction. Python's sqlite3
  module does not open one before a SAVEPOINT, so releasing the savepoint would otherwise
  commit immediately and a later rollback could not undo it.

Plain reads still run outside a transaction (Python's default), so a long read never blocks
a writer and never fails because another connection committed in the meantime.
"""

from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, make_url
from sqlalchemy.orm import Session, sessionmaker

from backend.core.config import get_settings

SQLITE_BUSY_TIMEOUT_MS = 30_000


def sqlite_file(url: str) -> Path | None:
    """The database file of a SQLite URL (None for other databases and in-memory SQLite)."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or parsed.database in (None, "", ":memory:"):
        return None
    return Path(parsed.database)


def ensure_sqlite_dir(url: str) -> None:
    """SQLite creates the file but not its folder (data/ is not in git)."""
    path = sqlite_file(url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)


def build_engine(url: str, echo: bool = False, **extra: Any) -> Engine:
    kwargs: dict[str, Any] = {"echo": echo, "future": True, **extra}
    sqlite = url.startswith("sqlite")
    if sqlite:
        kwargs.setdefault("connect_args", {"check_same_thread": False})
        ensure_sqlite_dir(url)
    else:
        kwargs["pool_pre_ping"] = True
    engine = create_engine(url, **kwargs)
    if sqlite:
        configure_sqlite(engine, wal=sqlite_file(url) is not None)
    return engine


def configure_sqlite(engine: Engine, wal: bool = True) -> None:
    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):  # type: ignore[no-untyped-def]
        dbapi_conn.execute("PRAGMA foreign_keys=ON")
        dbapi_conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        if wal:
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA synchronous=NORMAL")

    @event.listens_for(engine, "savepoint")
    def _begin_before_savepoint(conn, _name):  # type: ignore[no-untyped-def]
        dbapi_conn = conn.connection.dbapi_connection
        if dbapi_conn is not None and not dbapi_conn.in_transaction:
            conn.exec_driver_sql("BEGIN")


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return build_engine(settings.database_url, settings.database_echo)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
