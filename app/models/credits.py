"""Credits — the metering layer for paid recruiter actions.

Two tables rather than a single counter on the user:

* `credit_accounts` holds the balance, which is the only value read on the hot
  path and the only one that has to be updated atomically.
* `credit_transactions` is an append-only ledger. A balance nobody can explain
  is a support ticket waiting to happen, so every movement records what it was
  for, what it touched, and the balance it produced.

`idempotency_key` is what stops a recruiter being billed twice for the same
thing — revealing a contact they already paid for must not charge again.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base, created_col, updated_col, uuid_fk, uuid_pk

# What each metered action costs. Kept here so pricing lives with the model
# rather than being scattered through the routers.
COST_REVEAL_CONTACT = "reveal_contact"

# Revealing a candidate's contact is the only metered action: one credit buys
# that candidate, permanently. Outreach and résumé views are free because the
# recruiter already paid for the person.
DEFAULT_COSTS: dict[str, int] = {
    COST_REVEAL_CONTACT: 1,
}

REASONS = ("grant", "signup_bonus", "purchase", "spend", "refund", "adjustment")


class CreditAccount(Base):
    __tablename__ = "credit_accounts"

    account_id: Mapped[str] = uuid_pk()
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id"), unique=True, index=True, nullable=False
    )
    balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lifetime_granted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lifetime_spent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()

    transactions: Mapped[list["CreditTransaction"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"

    txn_id: Mapped[str] = uuid_pk()
    account_id: Mapped[str] = mapped_column(
        ForeignKey("credit_accounts.account_id", ondelete="CASCADE"),
        index=True, nullable=False,
    )
    user_id: Mapped[str] = uuid_fk("users.user_id")
    # Negative for spend, positive for grants and refunds.
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(30), index=True)
    action: Mapped[Optional[str]] = mapped_column(String(40), index=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(40))
    entity_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    # Unique per charge that must happen at most once (e.g. one reveal of one
    # profile by one recruiter). NULL for movements that may legitimately repeat.
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(160), unique=True, index=True)
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = created_col()

    account: Mapped[CreditAccount] = relationship(back_populates="transactions")
