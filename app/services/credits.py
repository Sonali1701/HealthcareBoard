"""Credit accounting.

Two properties matter more than anything else here, because getting either
wrong means charging a customer incorrectly:

**Atomicity.** The balance is decremented with a single conditional UPDATE
(`SET balance = balance - :cost WHERE user_id = :u AND balance >= :cost`).
Postgres takes a row lock for the duration, so two concurrent reveals racing
for the last credit cannot both succeed — one updates zero rows and is told
it has insufficient credit. A read-then-write in Python could not promise that.

**Idempotency.** Charges that must happen at most once pass an
`idempotency_key`. It carries a UNIQUE index, so a replayed request loses the
insert race and is refunded within the same transaction. Revealing a contact
you already paid for is free, forever.
"""
from __future__ import annotations

import logging

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..database import utcnow
from ..models import DEFAULT_COSTS, CreditAccount, CreditTransaction

logger = logging.getLogger("healthboard.credits")


class InsufficientCredits(Exception):
    """Raised when an account cannot cover a charge."""

    def __init__(self, needed: int, balance: int):
        self.needed, self.balance = needed, balance
        super().__init__(f"needs {needed} credits, has {balance}")


def cost_of(action: str) -> int:
    """Price for an action, overridable from settings (e.g. credit_cost_reveal_contact)."""
    override = getattr(settings, f"credit_cost_{action}", None)
    if isinstance(override, int) and override >= 0:
        return override
    return DEFAULT_COSTS.get(action, 0)


def get_account(db: Session, user_id: str, *, create: bool = True) -> CreditAccount | None:
    account = db.scalar(select(CreditAccount).where(CreditAccount.user_id == user_id))
    if account or not create:
        return account
    account = CreditAccount(
        user_id=user_id,
        balance=int(getattr(settings, "credit_signup_bonus", 0) or 0),
    )
    account.lifetime_granted = account.balance
    db.add(account)
    try:
        db.flush()
    except IntegrityError:            # another request created it first
        db.rollback()
        return db.scalar(select(CreditAccount).where(CreditAccount.user_id == user_id))
    if account.balance:
        db.add(CreditTransaction(
            account_id=account.account_id, user_id=user_id,
            delta=account.balance, balance_after=account.balance,
            reason="signup_bonus", note="Starting credits",
        ))
    return account


def balance(db: Session, user_id: str) -> int:
    account = get_account(db, user_id, create=False)
    return account.balance if account else 0


def already_charged(db: Session, key: str) -> bool:
    return bool(db.scalar(
        select(CreditTransaction.txn_id)
        .where(CreditTransaction.idempotency_key == key)))


def charge(db: Session, user_id: str, action: str, *, entity_type: str | None = None,
           entity_id: str | None = None, idempotency_key: str | None = None,
           note: str | None = None, cost: int | None = None) -> dict:
    """Debit an account for one metered action.

    Returns {charged, cost, balance}. `charged` is False when the action was
    free or had already been paid for. Raises InsufficientCredits otherwise.
    The caller commits — the debit joins whatever transaction it belongs to, so
    a failure downstream rolls the charge back with it.
    """
    price = cost_of(action) if cost is None else cost
    account = get_account(db, user_id)
    if price <= 0:
        return {"charged": False, "cost": 0, "balance": account.balance, "free": True}

    if idempotency_key and already_charged(db, idempotency_key):
        return {"charged": False, "cost": 0, "balance": account.balance,
                "already_paid": True}

    # Conditional decrement: the WHERE clause is the guard, so this is safe
    # under concurrency without an explicit SELECT ... FOR UPDATE.
    updated = db.execute(
        text("UPDATE credit_accounts SET balance = balance - :cost, "
             "lifetime_spent = lifetime_spent + :cost, updated_at = :now "
             "WHERE user_id = :uid AND balance >= :cost"),
        {"cost": price, "uid": user_id, "now": utcnow()},
    ).rowcount
    if not updated:
        db.refresh(account)
        raise InsufficientCredits(price, account.balance)

    db.refresh(account)
    txn = CreditTransaction(
        account_id=account.account_id, user_id=user_id,
        delta=-price, balance_after=account.balance,
        reason="spend", action=action, entity_type=entity_type,
        entity_id=entity_id, idempotency_key=idempotency_key, note=note,
    )
    db.add(txn)
    try:
        db.flush()
    except IntegrityError:
        # Lost an idempotency race: undo the debit rather than double-charge.
        db.rollback()
        db.execute(
            text("UPDATE credit_accounts SET balance = balance + :cost, "
                 "lifetime_spent = lifetime_spent - :cost WHERE user_id = :uid"),
            {"cost": price, "uid": user_id})
        db.commit()
        return {"charged": False, "cost": 0, "balance": balance(db, user_id),
                "already_paid": True}
    return {"charged": True, "cost": price, "balance": account.balance}


def grant(db: Session, user_id: str, amount: int, *, reason: str = "grant",
          note: str | None = None, granted_by: str | None = None) -> dict:
    """Add credits. Positive amounts only — use `charge` to take them away."""
    if amount <= 0:
        raise ValueError("grant amount must be positive")
    account = get_account(db, user_id)
    db.execute(
        text("UPDATE credit_accounts SET balance = balance + :n, "
             "lifetime_granted = lifetime_granted + :n, updated_at = :now "
             "WHERE user_id = :uid"),
        {"n": amount, "uid": user_id, "now": utcnow()})
    db.refresh(account)
    db.add(CreditTransaction(
        account_id=account.account_id, user_id=user_id, delta=amount,
        balance_after=account.balance, reason=reason,
        note=note or (f"Granted by {granted_by}" if granted_by else None)))
    return {"granted": amount, "balance": account.balance}


def refund(db: Session, user_id: str, amount: int, *, action: str | None = None,
           entity_id: str | None = None, note: str | None = None) -> dict:
    account = get_account(db, user_id)
    # CASE rather than GREATEST: the app supports SQLite as well as Postgres,
    # and GREATEST does not exist there.
    db.execute(
        text("UPDATE credit_accounts SET balance = balance + :n, "
             "lifetime_spent = CASE WHEN lifetime_spent - :n < 0 THEN 0 "
             "                      ELSE lifetime_spent - :n END, "
             "updated_at = :now WHERE user_id = :uid"),
        {"n": amount, "uid": user_id, "now": utcnow()})
    db.refresh(account)
    db.add(CreditTransaction(
        account_id=account.account_id, user_id=user_id, delta=amount,
        balance_after=account.balance, reason="refund", action=action,
        entity_id=entity_id, note=note))
    return {"refunded": amount, "balance": account.balance}
