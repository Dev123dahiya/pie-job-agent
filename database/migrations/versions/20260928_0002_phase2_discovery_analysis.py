"""phase 2: discovery runs, dedupe keys, structured job analysis

- jobs: canonical_url + fingerprint (secondary dedupe keys), last_seen_at, times_seen,
  discovery_run_id, last_error
- agent_runs: run_type, discovery parameters and progress counters, heartbeat
- job_analyses: renamed to the Phase 2 schema (skills_match, should_shortlist, reason),
  plus concerns, provenance (analyzer, prompt_version, input_snapshot) and rule results
- data: MatchLevel values acceptable->medium, mismatch->weak; freshness moves to
  fresh/stale/unknown

Revision ID: 0002_phase2
Revises: 0b7bbd5917e4
Create Date: 2026-09-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_phase2"
down_revision: str | None = "0b7bbd5917e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
MATCH_LEVEL = sa.Enum(
    "strong", "medium", "weak", "unknown", name="matchlevel", native_enum=False, length=40
)
LEVEL_COLUMNS = ("role_match", "skills_match", "experience_match", "location_match", "salary_match")


def upgrade() -> None:
    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "run_type",
                sa.Enum(
                    "discovery", "analysis", "mock", name="runtype", native_enum=False, length=40
                ),
                server_default="mock",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("source", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("search_terms", JSON, nullable=True))
        batch_op.add_column(sa.Column("locations", JSON, nullable=True))
        batch_op.add_column(sa.Column("current_task", sa.Text(), nullable=True))
        for name in (
            "searches_total",
            "searches_completed",
            "jobs_found",
            "jobs_new",
            "jobs_duplicate",
            "jobs_failed",
            "jobs_analyzed",
            "jobs_shortlisted",
            "jobs_rejected",
        ):
            batch_op.add_column(sa.Column(name, sa.Integer(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table("job_analyses", schema=None) as batch_op:
        batch_op.alter_column(
            "skill_match",
            new_column_name="skills_match",
            existing_type=sa.String(40),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "should_apply",
            new_column_name="should_shortlist",
            existing_type=sa.Boolean(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "recommendation_reason",
            new_column_name="reason",
            existing_type=sa.Text(),
            existing_nullable=True,
        )
        batch_op.add_column(sa.Column("concerns", JSON, nullable=True))
        batch_op.add_column(sa.Column("analyzer", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("prompt_version", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("input_snapshot", JSON, nullable=True))
        batch_op.add_column(sa.Column("shortlisted", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("rule_results", JSON, nullable=True))

    # --- data: new enum vocabularies, defaults for new NOT NULL JSON columns ---
    for column in LEVEL_COLUMNS:
        op.execute(f"UPDATE job_analyses SET {column} = 'medium' WHERE {column} = 'acceptable'")
        op.execute(f"UPDATE job_analyses SET {column} = 'weak' WHERE {column} = 'mismatch'")
    op.execute(
        "UPDATE job_analyses SET freshness = CASE freshness "
        "WHEN 'strong' THEN 'fresh' WHEN 'acceptable' THEN 'fresh' "
        "WHEN 'weak' THEN 'stale' WHEN 'mismatch' THEN 'stale' ELSE 'unknown' END"
    )
    op.execute("UPDATE job_analyses SET concerns = '[]' WHERE concerns IS NULL")
    op.execute("UPDATE job_analyses SET rule_results = '[]' WHERE rule_results IS NULL")

    with op.batch_alter_table("job_analyses", schema=None) as batch_op:
        batch_op.alter_column("concerns", existing_type=JSON, nullable=False)
        batch_op.alter_column("rule_results", existing_type=JSON, nullable=False)

    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("canonical_url", sa.String(length=2048), nullable=True))
        batch_op.add_column(sa.Column("fingerprint", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("times_seen", sa.Integer(), server_default="1", nullable=False)
        )
        batch_op.add_column(sa.Column("discovery_run_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("last_error", sa.Text(), nullable=True))
        batch_op.create_index(batch_op.f("ix_jobs_canonical_url"), ["canonical_url"], unique=False)
        batch_op.create_index(batch_op.f("ix_jobs_fingerprint"), ["fingerprint"], unique=False)
        batch_op.create_foreign_key(
            batch_op.f("fk_jobs_discovery_run_id_agent_runs"),
            "agent_runs",
            ["discovery_run_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_jobs_discovery_run_id_agent_runs"), type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_jobs_fingerprint"))
        batch_op.drop_index(batch_op.f("ix_jobs_canonical_url"))
        for name in (
            "last_error",
            "discovery_run_id",
            "times_seen",
            "last_seen_at",
            "fingerprint",
            "canonical_url",
        ):
            batch_op.drop_column(name)

    for column in LEVEL_COLUMNS:
        op.execute(f"UPDATE job_analyses SET {column} = 'acceptable' WHERE {column} = 'medium'")
    op.execute(
        "UPDATE job_analyses SET freshness = CASE freshness "
        "WHEN 'fresh' THEN 'strong' WHEN 'stale' THEN 'weak' ELSE 'unknown' END"
    )

    with op.batch_alter_table("job_analyses", schema=None) as batch_op:
        for name in (
            "rule_results",
            "shortlisted",
            "input_snapshot",
            "prompt_version",
            "analyzer",
            "concerns",
        ):
            batch_op.drop_column(name)
        batch_op.alter_column(
            "reason",
            new_column_name="recommendation_reason",
            existing_type=sa.Text(),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "should_shortlist",
            new_column_name="should_apply",
            existing_type=sa.Boolean(),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "skills_match",
            new_column_name="skill_match",
            existing_type=sa.String(40),
            existing_nullable=False,
        )

    with op.batch_alter_table("agent_runs", schema=None) as batch_op:
        for name in (
            "heartbeat_at",
            "jobs_rejected",
            "jobs_shortlisted",
            "jobs_analyzed",
            "jobs_failed",
            "jobs_duplicate",
            "jobs_new",
            "jobs_found",
            "searches_completed",
            "searches_total",
            "current_task",
            "locations",
            "search_terms",
            "source",
            "run_type",
        ):
            batch_op.drop_column(name)
