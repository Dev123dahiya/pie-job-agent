"""phase 3: autonomous agent core

- agent_runs: mode, iterations, max_iterations, pending_user_input
- agent_actions: tool, iteration, idempotency_key
- data: consolidate pipeline states into the Phase 3 state machine
  (RESUME_PREPARED/APPLICATION_STARTED -> READY_TO_APPLY;
   REQUIRES_USER_INPUT/CAPTCHA_REQUIRED/AUTH_REQUIRED -> BLOCKED_NEEDS_USER).
  New enum values (DRAFT, NEEDS_USER_INPUT, agent run type, events) need no DDL: enums
  are stored as VARCHAR.

Revision ID: 0003_phase3
Revises: 0002_phase2
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_phase3"
down_revision: str | None = "0002_phase2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATE_MAP = {
    "RESUME_PREPARED": "READY_TO_APPLY",
    "APPLICATION_STARTED": "READY_TO_APPLY",
    "REQUIRES_USER_INPUT": "BLOCKED_NEEDS_USER",
    "CAPTCHA_REQUIRED": "BLOCKED_NEEDS_USER",
    "AUTH_REQUIRED": "BLOCKED_NEEDS_USER",
}


def upgrade() -> None:
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("mode", sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column("iterations", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.add_column(sa.Column("max_iterations", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("pending_user_input", sa.Text(), nullable=True))

    with op.batch_alter_table("agent_actions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tool", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("iteration", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("idempotency_key", sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f("ix_agent_actions_tool"), ["tool"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_agent_actions_idempotency_key"), ["idempotency_key"], unique=False
        )

    for old, new in STATE_MAP.items():
        op.execute(f"UPDATE jobs SET pipeline_state = '{new}' WHERE pipeline_state = '{old}'")


def downgrade() -> None:
    # READY_TO_APPLY / BLOCKED_NEEDS_USER map back to the closest Phase 2 states.
    op.execute(
        "UPDATE jobs SET pipeline_state = 'RESUME_PREPARED' WHERE pipeline_state = 'READY_TO_APPLY'"
    )
    op.execute(
        "UPDATE jobs SET pipeline_state = 'REQUIRES_USER_INPUT' WHERE pipeline_state = 'BLOCKED_NEEDS_USER'"
    )

    with op.batch_alter_table("agent_actions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_agent_actions_idempotency_key"))
        batch_op.drop_index(batch_op.f("ix_agent_actions_tool"))
        batch_op.drop_column("idempotency_key")
        batch_op.drop_column("iteration")
        batch_op.drop_column("tool")

    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.drop_column("pending_user_input")
        batch_op.drop_column("max_iterations")
        batch_op.drop_column("iterations")
        batch_op.drop_column("mode")
