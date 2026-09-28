"""Per-user credits for Medhunt candidate-enrichment lookups."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base, created_col, updated_col, uuid_fk, uuid_pk


class MedhuntCreditAccount(Base):
    __tablename__ = "medhunt_credit_accounts"

    account_id: Mapped[str] = uuid_pk()
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id"), unique=True, index=True, nullable=False
    )
    balance: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    lifetime_granted: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    lifetime_spent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()

    transactions: Mapped[list["MedhuntCreditTransaction"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class MedhuntCreditTransaction(Base):
    __tablename__ = "medhunt_credit_transactions"

    txn_id: Mapped[str] = uuid_pk()
    account_id: Mapped[str] = mapped_column(
        ForeignKey("medhunt_credit_accounts.account_id", ondelete="CASCADE"),
        index=True, nullable=False,
    )
    user_id: Mapped[str] = uuid_fk("users.user_id")
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(30), index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(160), unique=True, index=True)
    note: Mapped[Optional[str]] = mapped_column(Text)
    actor_user_id: Mapped[Optional[str]] = uuid_fk("users.user_id", nullable=True)
    created_at: Mapped[datetime] = created_col()

    account: Mapped[MedhuntCreditAccount] = relationship(back_populates="transactions")
