"""Dashboard columns, job events, audit log, and export records.

Revision ID: 0002_dashboard
Revises: 0001_initial
Create Date: 2026-10-25
"""

from alembic import op
from sqlalchemy import inspect, text

from database.models import Base

revision = "0002_dashboard"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind)
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    statements: list[str] = []
    if "workers" in tables:
        have = {column["name"] for column in inspector.get_columns("workers")}
        if "scheduling_mode" not in have:
            statements.append(
                "ALTER TABLE workers ADD COLUMN scheduling_mode VARCHAR(16) NOT NULL DEFAULT 'enabled'"
            )
        if "policy_max_parallel" not in have:
            statements.append("ALTER TABLE workers ADD COLUMN policy_max_parallel INTEGER")
    if "jobs" in tables:
        have = {column["name"] for column in inspector.get_columns("jobs")}
        if "log_excerpt" not in have:
            statements.append("ALTER TABLE jobs ADD COLUMN log_excerpt TEXT")
    for statement in statements:
        bind.execute(text(statement))


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("export_records", "audit_events", "job_events"):
        bind.execute(text(f"DROP TABLE IF EXISTS {table}"))
