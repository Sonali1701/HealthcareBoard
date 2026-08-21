"""Credit balance, ledger, purchases (Stripe) and admin grants."""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..config import settings
from ..deps import CurrentUser, DbSession
from ..models import DEFAULT_COSTS, CreditAccount, CreditTransaction, User
from ..services import credits as credit_service

logger = logging.getLogger("healthboard.payments")

router = APIRouter(prefix="/api/credits", tags=["credits"])

# Purchasable credit packs. Prices are in cents (USD). Adjust freely — the
# credit amount is carried in the Stripe session metadata, so changing a price
# never affects a purchase already in flight.
CREDIT_PACKS = {
    "starter": {"credits": 50, "price_cents": 4900, "label": "Starter"},
    "growth": {"credits": 150, "price_cents": 12900, "label": "Growth"},
    "scale": {"credits": 500, "price_cents": 39900, "label": "Scale"},
}


def _payments_ready() -> bool:
    return bool(settings.payments_enabled and settings.stripe_secret_key)


class GrantIn(BaseModel):
    user_email: Optional[str] = None
    amount: int = Field(gt=0, le=1_000_000)
    note: Optional[str] = None


class CheckoutIn(BaseModel):
    pack_id: str


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


# --- Purchases (Stripe Checkout) ------------------------------------------

@router.get("/packs")
def credit_packs(user: CurrentUser):
    """The buyable packs and whether purchasing is switched on."""
    return {
        "enabled": _payments_ready(),
        "packs": [{"pack_id": k, "credits": v["credits"],
                   "price_cents": v["price_cents"], "label": v["label"]}
                  for k, v in CREDIT_PACKS.items()],
    }


@router.post("/checkout")
def create_checkout(body: CheckoutIn, user: CurrentUser):
    """Start a Stripe Checkout session for a credit pack; return its URL."""
    if not _payments_ready():
        raise HTTPException(status_code=503,
                            detail="Credit purchases aren't set up yet. Contact your administrator.")
    pack = CREDIT_PACKS.get(body.pack_id)
    if not pack:
        raise HTTPException(status_code=400, detail="Unknown credit pack")
    try:
        import stripe
    except ImportError:  # pragma: no cover
        raise HTTPException(status_code=503, detail="Payment library is not installed")
    stripe.api_key = settings.stripe_secret_key
    base = settings.frontend_base_url.rstrip("/")
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": f"{pack['credits']} HealthBoard credits"},
                    "unit_amount": pack["price_cents"],
                },
                "quantity": 1,
            }],
            customer_email=user.email,
            client_reference_id=user.user_id,
            metadata={"user_id": user.user_id, "pack_id": body.pack_id,
                      "credits": pack["credits"]},
            success_url=f"{base}/?page=credits&purchase=success",
            cancel_url=f"{base}/?page=credits&purchase=cancel",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Stripe checkout session failed for %s", user.user_id)
        raise HTTPException(status_code=502, detail="Could not start checkout. Try again shortly.")
    return {"url": session.url}


@router.post("/webhook", include_in_schema=False)
async def stripe_webhook(request: Request, db: DbSession):
    """Stripe payment callback. Grants credits once a session is paid.

    Verified by signature when a webhook secret is configured. Grants are keyed
    on the session id, so a redelivered event never credits an account twice.
    """
    if not _payments_ready():
        raise HTTPException(status_code=503, detail="Payments not configured")
    payload = await request.body()
    if settings.stripe_webhook_secret:
        try:
            import stripe
            event = stripe.Webhook.construct_event(
                payload, request.headers.get("stripe-signature"),
                settings.stripe_webhook_secret)
        except Exception:  # noqa: BLE001 — bad signature or malformed body
            raise HTTPException(status_code=400, detail="Invalid webhook signature")
    else:
        # No signing secret (local/dev): accept the JSON as-is. Never run a
        # production webhook without STRIPE_WEBHOOK_SECRET set.
        try:
            event = json.loads(payload)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid payload")

    if (event.get("type") == "checkout.session.completed"):
        session = event["data"]["object"]
        if session.get("payment_status") == "paid":
            meta = session.get("metadata") or {}
            user_id = meta.get("user_id") or session.get("client_reference_id")
            try:
                amount = int(meta.get("credits") or 0)
            except (TypeError, ValueError):
                amount = 0
            if user_id and amount > 0:
                credit_service.grant_once(
                    db, user_id, amount, idempotency_key=f"stripe:{session['id']}",
                    reason="purchase", note=f"Purchased {amount} credits")
                db.commit()
    return {"received": True}


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
