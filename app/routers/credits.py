"""Credit balance, ledger and admin grants."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..config import settings
from ..deps import CurrentUser, DbSession
from ..models import DEFAULT_COSTS, CreditAccount, CreditTransaction, User
from ..services import credits as credit_service

router = APIRouter(prefix="/api/credits", tags=["credits"])


class GrantIn(BaseModel):
    user_email: Optional[str] = None
    amount: int = Field(gt=0, le=1_000_000)
    note: Optional[str] = None


@router.get("")
def my_credits(user: CurrentUser, db: DbSession):
    """Balance plus the current price list, so the UI can warn before an action."""
    account = credit_service.get_account(db, user.user_id)
    db.commit()
    return {
        "balance": account.balance,
        "lifetime_granted": account.lifetime_granted,
        "lifetime_spent": account.lifetime_spent,
        "enabled": settings.credits_enabled,
        "costs": {a: credit_service.cost_of(a) for a in DEFAULT_COSTS},
    }


@router.get("/transactions")
def my_transactions(user: CurrentUser, db: DbSession,
                    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    rows = db.scalars(
        select(CreditTransaction)
        .where(CreditTransaction.user_id == user.user_id)
        .order_by(CreditTransaction.created_at.desc())
        .limit(limit).offset(offset)
    ).all()
    total = db.scalar(select(func.count()).select_from(CreditTransaction)
                      .where(CreditTransaction.user_id == user.user_id)) or 0
    return {"items": [{
        "txn_id": t.txn_id, "delta": t.delta, "balance_after": t.balance_after,
        "reason": t.reason, "action": t.action, "entity_id": t.entity_id,
        "note": t.note, "created_at": t.created_at,
    } for t in rows], "total": total}


@router.get("/usage")
def usage_summary(user: CurrentUser, db: DbSession):
    """What the credits went on, grouped by action."""
    rows = db.execute(
        select(CreditTransaction.action, func.count(), func.sum(CreditTransaction.delta))
        .where(CreditTransaction.user_id == user.user_id,
               CreditTransaction.reason == "spend")
        .group_by(CreditTransaction.action)
        .order_by(func.sum(CreditTransaction.delta))
    ).all()
    return {"by_action": [{"action": a or "other", "count": n, "credits": abs(int(s or 0))}
                          for a, n, s in rows]}


@router.post("/grant")
def grant_credits(body: GrantIn, user: CurrentUser, db: DbSession):
    """Add credits to an account. Admin only; self-service top-ups would be a
    payment integration, which this deliberately is not."""
    if user.role.value != "admin":
        raise HTTPException(status_code=403, detail="Only an admin can grant credits")
    target = user
    if body.user_email:
        target = db.scalar(select(User).where(
            func.lower(User.email) == body.user_email.strip().lower()))
        if not target:
            raise HTTPException(status_code=404, detail="No user with that email")
    result = credit_service.grant(db, target.user_id, body.amount,
                                  note=body.note, granted_by=user.email)
    db.commit()
    return {"user_email": target.email, **result}


@router.get("/accounts")
def list_accounts(user: CurrentUser, db: DbSession, limit: int = Query(50, ge=1, le=200)):
    """Every recruiter's balance — admin view for topping people up."""
    if user.role.value != "admin":
        raise HTTPException(status_code=403, detail="Admins only")
    rows = db.execute(
        select(CreditAccount, User.email)
        .join(User, User.user_id == CreditAccount.user_id)
        .order_by(CreditAccount.balance.asc()).limit(limit)
    ).all()
    return {"items": [{
        "user_email": email, "balance": a.balance,
        "lifetime_granted": a.lifetime_granted, "lifetime_spent": a.lifetime_spent,
    } for a, email in rows]}
