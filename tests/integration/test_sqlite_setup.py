"""SQLite is the default database (REQUIREMENTS 2): it must behave like PostgreSQL where PIE
relies on it, and cope with the daily run's heartbeat writing next to the main work."""

import threading
import time
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text
from sqlalchemy.orm import Session

from backend.core.config import REPO_ROOT, Settings
from database.session import build_engine, sqlite_file


@pytest.fixture
def file_engine(tmp_path: Path) -> Engine:
    engine = build_engine(f"sqlite+pysqlite:///{(tmp_path / 'sub' / 'pie.db').as_posix()}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
    return engine


def _count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM t")).scalar_one())


def test_default_database_is_a_sqlite_file_under_data() -> None:
    url = Settings.model_fields["database_url"].default  # tests set DATABASE_URL themselves
    path = sqlite_file(url)
    assert path is not None and path == REPO_ROOT / "data" / "pie.db"


def test_folder_is_created_and_wal_is_on(file_engine: Engine, tmp_path: Path) -> None:
    assert (tmp_path / "sub" / "pie.db").is_file()
    with file_engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar_one() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert conn.execute(text("PRAGMA busy_timeout")).scalar_one() >= 10_000


def test_released_savepoint_is_undone_by_the_outer_rollback(file_engine: Engine) -> None:
    """Without a real transaction around it, SQLite commits when the savepoint is released."""
    with Session(file_engine) as db:
        db.execute(text("SELECT 1"))  # reads only: no transaction open yet
        with db.begin_nested():
            db.execute(text("INSERT INTO t (v) VALUES ('kept only if committed')"))
        db.rollback()
    assert _count(file_engine) == 0


def test_rolled_back_savepoint_keeps_the_outer_work(file_engine: Engine) -> None:
    with Session(file_engine) as db:
        db.execute(text("INSERT INTO t (v) VALUES ('outer')"))
        with pytest.raises(RuntimeError), db.begin_nested():
            db.execute(text("INSERT INTO t (v) VALUES ('inner')"))
            raise RuntimeError("one job failed")
        db.commit()
    assert _count(file_engine) == 1


def test_heartbeat_writer_and_main_work_do_not_fail(file_engine: Engine) -> None:
    """The daily run's heartbeat thread writes every few seconds while the main thread reads,
    waits (e.g. for Claude) and then writes. Neither may fail with 'database is locked'."""
    errors: list[BaseException] = []
    stop = threading.Event()

    def heartbeat() -> None:
        while not stop.is_set():
            try:
                with Session(file_engine) as db:
                    db.execute(text("INSERT INTO t (v) VALUES ('beat')"))
                    db.commit()
            except BaseException as exc:
                errors.append(exc)
            time.sleep(0.01)

    thread = threading.Thread(target=heartbeat)
    thread.start()
    try:
        for i in range(8):
            with Session(file_engine) as db:
                db.execute(text("SELECT count(*) FROM t")).scalar_one()  # read
                time.sleep(0.05)  # slow step; the heartbeat commits meanwhile
                db.execute(text("INSERT INTO t (v) VALUES (:v)"), {"v": f"main {i}"})
                with db.begin_nested():
                    db.execute(text("INSERT INTO t (v) VALUES (:v)"), {"v": f"nested {i}"})
                db.commit()
    finally:
        stop.set()
        thread.join()
    assert errors == []
    with file_engine.connect() as conn:
        mains = conn.execute(text("SELECT count(*) FROM t WHERE v LIKE 'main %'")).scalar_one()
        nested = conn.execute(text("SELECT count(*) FROM t WHERE v LIKE 'nested %'")).scalar_one()
    assert mains == 8 and nested == 8


def test_dashboard_can_read_while_a_write_is_open(file_engine: Engine) -> None:
    with Session(file_engine) as writer:
        writer.execute(text("INSERT INTO t (v) VALUES ('uncommitted')"))
        started = time.monotonic()
        assert _count(file_engine) == 0  # sees the last committed state, without waiting
        assert time.monotonic() - started < 2
        writer.commit()
    assert _count(file_engine) == 1


def test_migrations_create_the_database_file(tmp_path: Path) -> None:
    db_file = tmp_path / "fresh" / "pie.db"
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_file.as_posix()}")
    command.upgrade(cfg, "head")
    engine = build_engine(f"sqlite+pysqlite:///{db_file.as_posix()}")
    assert {"jobs", "applications", "browser_sessions"} <= set(inspect(engine).get_table_names())
    engine.dispose()
