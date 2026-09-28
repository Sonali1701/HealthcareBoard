"""add organization-managed Medhunt SMS sender numbers

Revision ID: a6b7c8d9e0f1
Revises: f5a6b7c8d9e0
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a6b7c8d9e0f1"
down_revision: Union[str, None] = "f5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "medhunt_credit_accounts",
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("balance", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("lifetime_granted", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("lifetime_spent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("account_id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_index("ix_medhunt_credit_accounts_user_id", "medhunt_credit_accounts", ["user_id"])
    op.create_table(
        "medhunt_credit_transactions",
        sa.Column("txn_id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=30), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["medhunt_credit_accounts.account_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("txn_id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_medhunt_credit_transactions_account_id", "medhunt_credit_transactions", ["account_id"])
    op.create_index("ix_medhunt_credit_transactions_user_id", "medhunt_credit_transactions", ["user_id"])
    op.create_index("ix_medhunt_credit_transactions_reason", "medhunt_credit_transactions", ["reason"])
    op.create_index("ix_medhunt_credit_transactions_idempotency_key", "medhunt_credit_transactions", ["idempotency_key"])
    op.create_table(
        "medhunt_sms_senders",
        sa.Column("sender_id", sa.String(length=36), nullable=False),
        sa.Column("employer_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("sender_number", sa.String(length=40), nullable=False),
        sa.Column("zoom_user_id", sa.String(length=80), nullable=False),
        sa.Column("updated_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["employer_id"], ["employers.employer_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.user_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("sender_id"),
        sa.UniqueConstraint("employer_id", "user_id", name="uq_medhunt_sms_sender_org_user"),
        sa.UniqueConstraint("employer_id", "sender_number", name="uq_medhunt_sms_sender_org_number"),
    )
    op.create_index("ix_medhunt_sms_senders_employer_id", "medhunt_sms_senders", ["employer_id"])
    op.create_index("ix_medhunt_sms_senders_user_id", "medhunt_sms_senders", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_medhunt_sms_senders_user_id", table_name="medhunt_sms_senders")
    op.drop_index("ix_medhunt_sms_senders_employer_id", table_name="medhunt_sms_senders")
    op.drop_table("medhunt_sms_senders")
    op.drop_index("ix_medhunt_credit_transactions_idempotency_key", table_name="medhunt_credit_transactions")
    op.drop_index("ix_medhunt_credit_transactions_reason", table_name="medhunt_credit_transactions")
    op.drop_index("ix_medhunt_credit_transactions_user_id", table_name="medhunt_credit_transactions")
    op.drop_index("ix_medhunt_credit_transactions_account_id", table_name="medhunt_credit_transactions")
    op.drop_table("medhunt_credit_transactions")
    op.drop_index("ix_medhunt_credit_accounts_user_id", table_name="medhunt_credit_accounts")
    op.drop_table("medhunt_credit_accounts")
