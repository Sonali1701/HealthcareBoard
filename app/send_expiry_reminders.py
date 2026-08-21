"""Email clinicians before a licence or certification lapses.

Run daily (cron / Render scheduled job):
    python -m app.send_expiry_reminders

Reminders fire at fixed lead times (60/30/14/7/1 days) so a once-a-day run sends
each one exactly once — no per-credential "already reminded" state to track. Only
registered clinicians with an email are contacted; imported profiles are skipped.
"""
from __future__ import annotations

import sys
from datetime import date

from sqlalchemy import select

from .database import SessionLocal
from .models import Certification, License, Notification, Profile, User
from .models.enums import NotificationType
from .services.email import send_credential_expiry

# Days-before-expiry at which a reminder is sent.
THRESHOLDS = {60, 30, 14, 7, 1}


def run() -> dict:
    db = SessionLocal()
    emailed = notified = 0
    try:
        today = date.today()
        items: list[tuple[str, str, date]] = []
        for lic in db.scalars(select(License).where(License.expiry_date.is_not(None))):
            label = f"{lic.license_type} licence" + (f" ({lic.state_code})" if lic.state_code else "")
            items.append((lic.profile_id, label, lic.expiry_date))
        for c in db.scalars(select(Certification).where(Certification.expiry_date.is_not(None))):
            items.append((c.profile_id, f"{c.cert_name} certification", c.expiry_date))

        prof_ids = {pid for pid, _, _ in items}
        profiles = {p.profile_id: p for p in db.scalars(
            select(Profile).where(Profile.profile_id.in_(prof_ids)))} if prof_ids else {}
        owner_ids = [p.user_id for p in profiles.values() if p.user_id]
        users = {u.user_id: u for u in db.scalars(
            select(User).where(User.user_id.in_(owner_ids)))} if owner_ids else {}

        for pid, label, expiry in items:
            days = (expiry - today).days
            if days not in THRESHOLDS:
                continue
            prof = profiles.get(pid)
            if not prof or not prof.user_id:          # only registered clinicians
                continue
            user = users.get(prof.user_id)
            email = prof.email or (user.email if user else None)
            if not email:
                continue
            name = (prof.first_name or "").strip()
            if send_credential_expiry(email, name=name, credential=label,
                                      expiry_date=expiry, days_left=days):
                emailed += 1
            db.add(Notification(
                user_id=prof.user_id, type=NotificationType.system,
                title="Credential expiring soon",
                body=f"Your {label} expires in {days} day{'s' if days != 1 else ''} "
                     f"({expiry}). Renew it to stay placeable."))
            notified += 1
        db.commit()
        print(f"Expiry reminders: {emailed} emailed, {notified} in-app notifications.")
        return {"emailed": emailed, "notified": notified}
    finally:
        db.close()


def main() -> int:
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
