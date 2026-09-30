"""phase 4: resume tailoring - provenance, validation and reports on resume_versions

All columns are nullable additions: existing rows (none yet) are untouched.

Revision ID: 0004_phase4
Revises: 0003_phase3
Create Date: 2026-09-28

"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_phase4"
down_revision: str | None = "0003_phase3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
VALIDATION = sa.Enum(
    "MASTER", "VALIDATED", name="resumevalidationstatus", native_enum=False, length=16
)
NEW_COLUMNS: list[sa.Column[Any]] = [
    sa.Column("validation_status", VALIDATION, nullable=True),
    sa.Column("validation_report", JSON, nullable=True),
    sa.Column("tailoring_strategy", sa.String(length=32), nullable=True),
    sa.Column("artifact_format", sa.String(length=16), nullable=True),
    sa.Column("content", JSON, nullable=True),
    sa.Column("change_details", JSON, nullable=True),
    sa.Column("tailoring_report", JSON, nullable=True),
    sa.Column("source_sha256", sa.String(length=64), nullable=True),
    sa.Column("profile_sha256", sa.String(length=64), nullable=True),
    sa.Column("model", sa.String(length=128), nullable=True),
    sa.Column("prompt_version", sa.String(length=32), nullable=True),
]


def upgrade() -> None:
    with op.batch_alter_table("resume_versions", schema=None) as batch_op:
        for column in NEW_COLUMNS:
            batch_op.add_column(column)


def downgrade() -> None:
    with op.batch_alter_table("resume_versions", schema=None) as batch_op:
        for column in reversed(NEW_COLUMNS):
            batch_op.drop_column(column.name)
