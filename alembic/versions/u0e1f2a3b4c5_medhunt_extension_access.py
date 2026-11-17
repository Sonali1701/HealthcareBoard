"""Allow organization owners and admins to pause a member's extension access."""
from alembic import op
import sqlalchemy as sa

revision = "u0e1f2a3b4c5"
down_revision = "t9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "medhunt_extension_enabled" not in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")
    }:
        op.add_column("users", sa.Column("medhunt_extension_enabled", sa.Boolean(),
                                          nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("users", "medhunt_extension_enabled")
