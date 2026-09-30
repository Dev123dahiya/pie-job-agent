"""scrub stored answers: remove the user's answers from existing records

Before this revision, browser-session records kept the value PIE entered in each form field
(salary, notice period, contact details...), and copies of it reached the agent's action log
and some messages. PIE now stores only which answer key was used. This data migration cleans
the rows written before that change. No schema change.

- field records (anything with a ``field_id``) lose ``value``, ``option`` and ``selected``
- messages that quoted an answer ("verified answer 'Immediate' but ...") no longer quote it

Downgrade is a no-op: removed answers cannot (and should not) be restored.

Revision ID: 0008_scrub_answers
Revises: 0007_leads
Create Date: 2026-09-30 16:00:00.000000

"""

import re
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008_scrub_answers"
down_revision: str | None = "0007_leads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_TYPE = sa.JSON().with_variant(JSONB(), "postgresql")
PRIVATE_FIELD_KEYS = ("value", "option", "selected")

# Old message formats that quoted the answer, and what they become.
_QUOTED = [
    (re.compile(r"verified answer '[^']*' but"), "verified answer but"),
    (re.compile(r"the verified answer is '[^']*'"), "the verified answer is not a number"),
    (
        re.compile(r"the dropdown shows '[^']*' after selecting"),
        "the dropdown does not show the chosen option after selecting",
    ),  # fmt: skip
    (
        re.compile(r"the only option starting with '(?:yes|no)'"),
        "the only option starting with the answer's yes/no",
    ),  # fmt: skip
    (re.compile(r"options start with '(?:yes|no)'"), "options start with the answer's yes/no"),
    (re.compile(r"no option says '(?:yes|no)'"), "no option starts with the answer's yes/no"),
    # A truncated JSON copy of an old tool result kept inside a string.
    (re.compile(r'"(value|option|selected)": ?("(?:[^"\\]|\\.)*"|-?\d+|null)'), r'"\1": null'),
]

# table -> (JSON columns, text columns)
TARGETS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "browser_sessions": (
        ("detected_fields", "filled_fields", "missing_answers", "skipped_fields"),
        ("current_step", "user_action_required", "last_error"),
    ),
    "agent_actions": (("input", "output"), ("error",)),
    "external_action_logs": ((), ("summary", "error")),
    "application_events": (("evidence",), ("reason",)),
    "agent_runs": ((), ("summary", "pending_user_input")),
}


def scrub_text(text: str) -> str:
    for pattern, replacement in _QUOTED:
        text = pattern.sub(replacement, text)
    return text


def scrub(data: Any) -> Any:
    """A copy of ``data`` without answer values."""
    if isinstance(data, dict):
        is_field = "field_id" in data
        return {k: scrub(v) for k, v in data.items() if not (is_field and k in PRIVATE_FIELD_KEYS)}
    if isinstance(data, list):
        return [scrub(v) for v in data]
    if isinstance(data, str):
        return scrub_text(data)
    return data


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for name, (json_cols, text_cols) in TARGETS.items():
        present = {c["name"] for c in inspector.get_columns(name)}
        json_cols = tuple(c for c in json_cols if c in present)
        text_cols = tuple(c for c in text_cols if c in present)
        if not json_cols and not text_cols:
            continue
        table = sa.table(
            name,
            sa.column("id", sa.Uuid()),
            *(sa.column(c, JSON_TYPE) for c in json_cols),
            *(sa.column(c, sa.Text()) for c in text_cols),
        )
        rows = bind.execute(sa.select(table)).mappings().all()
        for row in rows:
            changes: dict[str, Any] = {}
            for col in json_cols:
                if row[col] is not None and (clean := scrub(row[col])) != row[col]:
                    changes[col] = clean
            for col in text_cols:
                if row[col] is not None and (clean := scrub_text(row[col])) != row[col]:
                    changes[col] = clean
            if changes:
                bind.execute(sa.update(table).where(table.c.id == row["id"]).values(**changes))


def downgrade() -> None:
    """Nothing to restore: the removed answers are gone on purpose."""
