"""add enterprise teams and messaging permissions

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organization_teams",
        sa.Column("team_id", sa.String(length=36), nullable=False),
        sa.Column("employer_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["employer_id"], ["employers.employer_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("team_id"),
        sa.UniqueConstraint("employer_id", "name", name="uq_organization_team_name"),
    )
    op.create_index("ix_organization_teams_employer_id", "organization_teams", ["employer_id"])
    op.create_index("ix_organization_teams_status", "organization_teams", ["status"])
    op.create_table(
        "organization_team_members",
        sa.Column("team_member_id", sa.String(length=36), nullable=False),
        sa.Column("team_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("team_role", sa.String(length=20), nullable=False, server_default="member"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["organization_teams.team_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("team_member_id"),
        sa.UniqueConstraint("team_id", "user_id", name="uq_organization_team_member"),
    )
    op.create_index("ix_organization_team_members_team_id", "organization_team_members", ["team_id"])
    op.create_index("ix_organization_team_members_user_id", "organization_team_members", ["user_id"])
    op.create_index("ix_organization_team_members_status", "organization_team_members", ["status"])
    op.create_table(
        "medhunt_messaging_permissions",
        sa.Column("permission_id", sa.String(length=36), nullable=False),
        sa.Column("employer_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="disabled"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("permission_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["employer_id"], ["employers.employer_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("permission_id"),
        sa.UniqueConstraint("employer_id", "user_id", name="uq_medhunt_messaging_permission"),
    )
    op.create_index("ix_medhunt_messaging_permissions_employer_id", "medhunt_messaging_permissions", ["employer_id"])
    op.create_index("ix_medhunt_messaging_permissions_user_id", "medhunt_messaging_permissions", ["user_id"])
    op.create_index("ix_medhunt_messaging_permissions_status", "medhunt_messaging_permissions", ["status"])
    op.create_table(
        "zoom_organization_integrations",
        sa.Column("integration_id", sa.String(length=36), nullable=False),
        sa.Column("employer_id", sa.String(length=36), nullable=False),
        sa.Column("zoom_account_id", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="connected"),
        sa.Column("access_token_encrypted", sa.Text(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
        sa.Column("access_token_expires_at", sa.DateTime(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("connected_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["employer_id"], ["employers.employer_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connected_by_user_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("integration_id"),
        sa.UniqueConstraint("employer_id", name="uq_zoom_organization_employer"),
    )
    op.create_index("ix_zoom_organization_account", "zoom_organization_integrations", ["zoom_account_id"])
    op.add_column("team_invites", sa.Column("team_id", sa.String(length=36), nullable=True))
    op.create_foreign_key(
        "fk_team_invites_team_id", "team_invites", "organization_teams",
        ["team_id"], ["team_id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_team_invites_team_id", "team_invites", type_="foreignkey")
    op.drop_column("team_invites", "team_id")
    op.drop_table("zoom_organization_integrations")
    op.drop_table("medhunt_messaging_permissions")
    op.drop_table("organization_team_members")
    op.drop_table("organization_teams")
