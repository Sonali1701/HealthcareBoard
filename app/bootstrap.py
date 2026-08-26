"""Startup bootstrap: ensure an admin account exists when configured."""
from __future__ import annotations

import logging

from sqlalchemy import select

from .database import SessionLocal, utcnow
from .config import settings
from .models import User
from .models.enums import UserRole, UserStatus
from .security import hash_password

logger = logging.getLogger("healthboard.bootstrap")


def ensure_admin() -> None:
    """Make ADMIN_EMAIL a platform admin, creating the account if needed.

    If no user has that email, one is created from ADMIN_EMAIL / ADMIN_PASSWORD.
    If the account already exists (e.g. the owner signed up normally), it is
    promoted to the admin role and activated — so pointing ADMIN_EMAIL at an
    existing account reliably grants it admin, rather than silently doing
    nothing. An existing password is never overwritten.
    """
    if not settings.admin_email or not settings.admin_password:
        return
    db = SessionLocal()
    try:
        existing = db.scalar(select(User).where(User.email == settings.admin_email))
        if existing:
            changed = False
            if existing.role != UserRole.admin:
                existing.role = UserRole.admin
                changed = True
            if existing.status != UserStatus.active:
                existing.status = UserStatus.active
                changed = True
            if existing.deleted_at is not None:
                existing.deleted_at = None
                changed = True
            if changed:
                db.commit()
                logger.info("Promoted existing user %s to admin", settings.admin_email)
            return
        db.add(User(
            email=settings.admin_email,
            password_hash=hash_password(settings.admin_password),
            role=UserRole.admin,
            status=UserStatus.active,
            email_verified_at=utcnow(),
        ))
        db.commit()
        logger.info("Bootstrapped admin user %s", settings.admin_email)
    finally:
        db.close()
