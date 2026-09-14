"""Weekly organization credit-usage reports.

The previous completed Monday-Sunday period is emailed to organization owners
and organization admins. Managers and ordinary members are intentionally not
recipients. A delivery ledger prevents duplicate successful sends across app
restarts and multiple Gunicorn workers.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..database import SessionLocal, utcnow
from ..models import (
    COST_REVEAL_CONTACT,
    CreditAccount,
    CreditTransaction,
    Employer,
    EmployerMember,
    Profile,
    User,
    UserStatus,
    WeeklyUsageReportDelivery,
)
from . import email as email_service
from . import org_roles

logger = logging.getLogger("healthboard.weekly_usage")


def report_period(now: datetime | None = None) -> tuple[date, date]:
    """Return the previous completed Monday-Monday window (end is exclusive)."""
    current = (now or utcnow()).astimezone(timezone.utc)
    this_monday = current.date() - timedelta(days=current.weekday())
    end = this_monday
    return end - timedelta(days=7), end


def reports_are_due(now: datetime | None = None) -> bool:
    """Wait until the configured UTC hour on Monday; catch up after that."""
    current = (now or utcnow()).astimezone(timezone.utc)
    this_monday = current.date() - timedelta(days=current.weekday())
    eligible_at = datetime.combine(
        this_monday,
        time(hour=max(0, min(23, settings.weekly_usage_email_hour_utc))),
        tzinfo=timezone.utc,
    )
    return current >= eligible_at


def _org_members(db, employer: Employer) -> tuple[list[dict], list[User]]:
    memberships = db.scalars(select(EmployerMember).where(
        EmployerMember.employer_id == employer.employer_id)).all()
    role_by = {m.user_id: org_roles.normalize_role(m.member_role) for m in memberships}
    role_by[employer.owner_user_id] = "owner"
    user_ids = list(role_by)
    if not user_ids:
        return [], []

    users = {u.user_id: u for u in db.scalars(
        select(User).where(User.user_id.in_(user_ids)))}
    profiles = {p.user_id: p for p in db.scalars(
        select(Profile).where(Profile.user_id.in_(user_ids)))}
    accounts = {a.user_id: a for a in db.scalars(
        select(CreditAccount).where(CreditAccount.user_id.in_(user_ids)))}

    rows = []
    for user_id, role in role_by.items():
        user = users.get(user_id)
        profile = profiles.get(user_id)
        account = accounts.get(user_id)
        rows.append({
            "user_id": user_id,
            "email": user.email if user else None,
            "name": (f"{profile.first_name or ''} {profile.last_name or ''}".strip()
                     if profile else None),
            "role": role,
            "role_label": org_roles.ROLE_LABELS.get(role, role.title()),
            "credits_remaining": account.balance if account else 0,
            "lifetime_spent": account.lifetime_spent if account else 0,
            "credits_used": 0,
            "contacts_revealed": 0,
        })

    recipients = [u for uid, role in role_by.items()
                  if role in {"owner", "admin"}
                  and (u := users.get(uid)) is not None
                  and u.status == UserStatus.active
                  and u.deleted_at is None
                  and bool(u.email)]
    return rows, recipients


def _add_period_usage(db, rows: list[dict], start: date, end: date) -> None:
    if not rows:
        return
    ids = [row["user_id"] for row in rows]
    start_at = datetime.combine(start, time.min, tzinfo=timezone.utc)
    end_at = datetime.combine(end, time.min, tzinfo=timezone.utc)
    usage = db.execute(
        select(
            CreditTransaction.user_id,
            func.coalesce(func.sum(CreditTransaction.delta), 0),
            func.count(CreditTransaction.txn_id),
        ).where(
            CreditTransaction.user_id.in_(ids),
            CreditTransaction.reason == "spend",
            CreditTransaction.created_at >= start_at,
            CreditTransaction.created_at < end_at,
        ).group_by(CreditTransaction.user_id)
    ).all()
    by_user = {uid: abs(int(delta or 0)) for uid, delta, _ in usage}
    reveals = dict(db.execute(
        select(CreditTransaction.user_id, func.count(CreditTransaction.txn_id)).where(
            CreditTransaction.user_id.in_(ids),
            CreditTransaction.reason == "spend",
            CreditTransaction.action == COST_REVEAL_CONTACT,
            CreditTransaction.created_at >= start_at,
            CreditTransaction.created_at < end_at,
        ).group_by(CreditTransaction.user_id)
    ).all())
    for row in rows:
        row["credits_used"] = by_user.get(row["user_id"], 0)
        row["contacts_revealed"] = int(reveals.get(row["user_id"], 0))
    rows.sort(key=lambda row: (-row["credits_used"], -org_roles.rank(row["role"]),
                               (row["name"] or row["email"] or "").lower()))


def _claim(db, employer_id: str, recipient_user_id: str, start: date,
           end: date, now: datetime) -> WeeklyUsageReportDelivery | None:
    existing = db.scalar(select(WeeklyUsageReportDelivery).where(
        WeeklyUsageReportDelivery.employer_id == employer_id,
        WeeklyUsageReportDelivery.recipient_user_id == recipient_user_id,
        WeeklyUsageReportDelivery.period_start == start,
    ))
    if existing:
        if existing.status == "sent":
            return None
        stale_before = now - timedelta(hours=1)
        changed = db.execute(update(WeeklyUsageReportDelivery).where(
            WeeklyUsageReportDelivery.delivery_id == existing.delivery_id,
            WeeklyUsageReportDelivery.status != "sent",
            or_(WeeklyUsageReportDelivery.status == "failed",
                WeeklyUsageReportDelivery.updated_at < stale_before),
        ).values(status="pending", period_end=end, updated_at=now)).rowcount
        db.commit()
        return existing if changed else None

    delivery = WeeklyUsageReportDelivery(
        employer_id=employer_id,
        recipient_user_id=recipient_user_id,
        period_start=start,
        period_end=end,
        status="pending",
    )
    db.add(delivery)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    return delivery


def run(*, now: datetime | None = None, session_factory=SessionLocal) -> dict:
    """Build and send all due reports; safe to invoke repeatedly."""
    current = (now or utcnow()).astimezone(timezone.utc)
    start, end = report_period(current)
    stats = {
        "organizations": 0,
        "recipients": 0,
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "period_start": str(start),
        "period_end": str(end),
    }
    db = session_factory()
    try:
        for employer in db.scalars(select(Employer).order_by(Employer.employer_id)):
            rows, recipients = _org_members(db, employer)
            if not recipients:
                continue
            stats["organizations"] += 1
            stats["recipients"] += len(recipients)
            _add_period_usage(db, rows, start, end)
            totals = {
                "credits_used": sum(r["credits_used"] for r in rows),
                "contacts_revealed": sum(r["contacts_revealed"] for r in rows),
                "credits_remaining": sum(r["credits_remaining"] for r in rows),
                "members": len(rows),
            }
            for recipient in recipients:
                delivery = _claim(
                    db, employer.employer_id, recipient.user_id, start, end, current
                )
                if not delivery:
                    stats["skipped"] += 1
                    continue
                ok = email_service.send_weekly_usage_report(
                    recipient.email,
                    org_name=employer.org_name,
                    period_start=start,
                    period_end=end,
                    members=rows,
                    totals=totals,
                )
                delivery.status = "sent" if ok else "failed"
                delivery.sent_at = current if ok else None
                delivery.updated_at = current
                db.commit()
                stats["sent" if ok else "failed"] += 1
        logger.info("Weekly usage report run: %s", stats)
        return stats
    finally:
        db.close()


async def scheduler() -> None:
    """Check periodically; the delivery ledger turns checks into one weekly send."""
    interval = max(300, int(settings.weekly_usage_email_check_seconds))
    while True:
        try:
            if (settings.weekly_usage_emails_enabled and settings.email_enabled
                    and settings.sendgrid_api_key and reports_are_due()):
                await asyncio.to_thread(run)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one failed cycle must not stop future weeks
            logger.exception("Weekly usage report scheduler failed")
        await asyncio.sleep(interval)
