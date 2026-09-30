"""Migrations must build the same schema the models declare.

Runs on SQLite always, and on PostgreSQL too when PIE_TEST_POSTGRES_URL points at an
empty, disposable database (e.g. the docker-compose one).
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from backend.core.config import REPO_ROOT
from database.base import Base


def _config(url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _check_round_trip(url: str) -> None:
    cfg = _config(url)

    command.upgrade(cfg, "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"Models and migrations differ: {diff}"
    assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())

    command.downgrade(cfg, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]
    engine.dispose()


def test_sqlite_upgrade_matches_models_and_downgrades(tmp_path: Path) -> None:
    _check_round_trip(f"sqlite+pysqlite:///{(tmp_path / 'm.db').as_posix()}")


@pytest.mark.skipif(not os.getenv("PIE_TEST_POSTGRES_URL"), reason="PIE_TEST_POSTGRES_URL not set")
def test_postgres_upgrade_matches_models_and_downgrades() -> None:
    _check_round_trip(os.environ["PIE_TEST_POSTGRES_URL"])
