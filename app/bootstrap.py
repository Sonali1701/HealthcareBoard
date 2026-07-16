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
    """Create the admin user from ADMIN_EMAIL / ADMIN_PASSWORD if not present."""
    if not settings.admin_email or not settings.admin_password:
        return
    db = SessionLocal()
    try:
        existing = db.scalar(select(User).where(User.email == settings.admin_email))
        if existing:
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
