"""Migration 0008 removes answers that older PIE versions stored (REQUIREMENTS 13)."""

import importlib.util
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from backend.core.config import REPO_ROOT

MIGRATION = next(
    (REPO_ROOT / "database/migrations/versions").glob("*_0008_scrub_stored_answers.py")
)


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("scrub_0008", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OLD_DETECTED = [
    {"field_id": "f0_0", "label": "Notice period", "key": "notice_period", "kind": "text",
     "value": "Immediate", "option": None, "selected": None, "options": []},
    {"field_id": "f0_1", "label": "Relocate?", "key": "willing_to_relocate", "kind": "select",
     "value": "Yes", "option": 1, "selected": "Yes", "options": ["Select...", "Yes", "No"]},
]  # fmt: skip
OLD_FILLED = [
    {"field_id": "f0_0", "label": "Notice period", "key": "notice_period",
     "value": "Immediate", "source": "profile", "method": "direct"},
]  # fmt: skip
OLD_MISSING = [
    {"field_id": "f0_2", "question": "Expected CTC", "question_key": "expected_salary",
     "reason": "verified answer '14 LPA' but no option exactly matches the verified answer"},
]  # fmt: skip


def test_scrub_removes_values_and_quoted_answers() -> None:
    m = _migration()
    assert m.scrub(OLD_DETECTED) == [
        {"field_id": "f0_0", "label": "Notice period", "key": "notice_period", "kind": "text",
         "options": []},
        {"field_id": "f0_1", "label": "Relocate?", "key": "willing_to_relocate", "kind": "select",
         "options": ["Select...", "Yes", "No"]},
    ]  # fmt: skip
    assert "14 LPA" not in json.dumps(m.scrub(OLD_MISSING))
    assert m.scrub_text("the dropdown shows 'Immediate' after selecting") == (
        "the dropdown does not show the chosen option after selecting"
    )
    truncated = '{"fields": [{"field_id": "f0_0", "value": "10 LPA", "option": 2}]'
    assert "10 LPA" not in m.scrub_text(truncated) and '"option": null' in m.scrub_text(truncated)
    # Non-field dictionaries keep a "value" key (e.g. a setting), only answers are removed.
    assert m.scrub({"value": 3}) == {"value": 3}


def _dummy(col: dict[str, Any]) -> Any:
    t = col["type"]
    if isinstance(t, sa.JSON):
        return json.dumps([])
    if isinstance(t, sa.Uuid) or col["name"] == "id" or col["name"].endswith("_id"):
        return uuid.uuid4().hex
    if isinstance(t, sa.DateTime):
        return datetime.now(UTC).isoformat()
    if isinstance(t, sa.Integer):
        return 0
    if isinstance(t, sa.Boolean):
        return False
    return "DRY_RUN" if col["name"] == "mode" else "x"


def test_upgrade_cleans_existing_session_rows(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{(tmp_path / 'm.db').as_posix()}"
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "0007_leads")

    engine = sa.create_engine(url)
    cols = sa.inspect(engine).get_columns("browser_sessions")
    row = {c["name"]: _dummy(c) for c in cols if not c["nullable"]}
    row |= {
        "detected_fields": json.dumps(OLD_DETECTED),
        "filled_fields": json.dumps(OLD_FILLED),
        "missing_answers": json.dumps(OLD_MISSING),
    }
    names = ", ".join(row)
    with engine.begin() as conn:  # a row as an older PIE wrote it (foreign keys are off)
        conn.execute(sa.text(f"INSERT INTO browser_sessions ({names}) VALUES "
                             f"({', '.join(':' + k for k in row)})"), row)  # fmt: skip

    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        stored = conn.execute(
            sa.text("SELECT detected_fields, filled_fields, missing_answers FROM browser_sessions")
        ).one()
    engine.dispose()
    text = " ".join(stored)
    for answer in ("Immediate", "14 LPA", '"value"', '"option"', '"selected"'):
        assert answer not in text, answer
    assert json.loads(stored[1])[0]["key"] == "notice_period"  # which answer was used stays

    command.downgrade(cfg, "0007_leads")  # the downgrade is a no-op and must not fail
