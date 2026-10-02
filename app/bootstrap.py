"""Startup bootstrap: ensure an admin account exists when configured."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .database import SessionLocal, utcnow
from .config import settings
from .models import (
    Employer,
    EmployerMember,
    MedhuntMessagingPermission,
    MedhuntSmsSender,
    OrganizationTeam,
    OrganizationTeamMember,
    User,
)
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


def ensure_enterprise_organization_data() -> None:
    """Backfill a safe default team and explicit permissions for live orgs.

    New tables are created by ``init_db``. This idempotent bootstrap handles
    existing organizations without requiring a maintenance window: members
    without any team join a General team, existing managers manage it, and
    users who already have a configured sender retain their messaging access.
    Members moved out of the legacy General team are never added back.
    """
    db = SessionLocal()
    try:
        changed = False
        for employer in db.scalars(select(Employer)).all():
            team = db.scalar(select(OrganizationTeam).where(
                OrganizationTeam.employer_id == employer.employer_id,
                OrganizationTeam.name == "General",
            ))
            if not team:
                team = OrganizationTeam(
                    employer_id=employer.employer_id,
                    name="General",
                    created_by_user_id=employer.owner_user_id,
                )
                db.add(team)
                db.flush()
                changed = True
            members = db.scalars(select(EmployerMember).where(
                EmployerMember.employer_id == employer.employer_id,
            )).all()
            employer_team_ids = list(db.scalars(select(OrganizationTeam.team_id).where(
                OrganizationTeam.employer_id == employer.employer_id,
            )).all())
            existing_team_users = set(db.scalars(select(OrganizationTeamMember.user_id).where(
                OrganizationTeamMember.team_id.in_(employer_team_ids),
            )).all()) if employer_team_ids else set()
            for member in members:
                if member.user_id in existing_team_users:
                    continue
                db.add(OrganizationTeamMember(
                    team_id=team.team_id,
                    user_id=member.user_id,
                    team_role="manager" if member.member_role == "manager" else "member",
                    created_by_user_id=employer.owner_user_id,
                ))
                changed = True
            sender_user_ids = set(db.scalars(select(MedhuntSmsSender.user_id).where(
                MedhuntSmsSender.employer_id == employer.employer_id,
            )).all())
            permitted_user_ids = set(db.scalars(select(MedhuntMessagingPermission.user_id).where(
                MedhuntMessagingPermission.employer_id == employer.employer_id,
            )).all())
            for user_id in sender_user_ids - permitted_user_ids:
                db.add(MedhuntMessagingPermission(
                    employer_id=employer.employer_id,
                    user_id=user_id,
                    status="enabled",
                    reason="Backfilled from existing Zoom sender assignment",
                    updated_by_user_id=employer.owner_user_id,
                ))
                changed = True
        if changed:
            try:
                db.commit()
                logger.info("Backfilled enterprise organization teams and messaging permissions")
            except IntegrityError:
                # Gunicorn workers boot concurrently. Another worker may have
                # completed the same idempotent backfill first.
                db.rollback()
                logger.info("Enterprise organization backfill was completed by another worker")
    finally:
        db.close()
