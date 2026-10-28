"""Initial coordinator schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-25
"""

from alembic import op

from database.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind)
