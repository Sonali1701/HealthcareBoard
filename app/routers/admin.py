"""Platform super-admin console — the owner's view of the whole job board.

Every endpoint here is gated by ``AdminUser`` (role == admin), so only the
platform owner (bootstrapped from ADMIN_EMAIL / ADMIN_PASSWORD, see
app/bootstrap.py) can reach it. It exposes read-only dashboard data plus a few
carefully guarded management actions:

  * GET  /api/admin/overview        — headline platform metrics
  * GET  /api/admin/users           — search / filter / paginate every account
  * PATCH /api/admin/users/{id}     — suspend, reactivate, or change a role
  * GET  /api/admin/organizations   — agencies/employers ("vendors") on the board

Management actions never touch the acting admin's own account and refuse to
remove the last remaining admin, so an owner can't accidentally lock the whole
platform out of its own admin.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from ..database import utcnow
from ..deps import AdminUser, DbSession
from ..models import (
    Application,
    AuditLog,
    CreditAccount,
    Employer,
    EmployerMember,
    JobPosting,
    Message,
    Profile,
    Session as UserSession,
    User,
)
from ..models.enums import JobStatus, UserRole, UserStatus

router = APIRouter(prefix="/api/admin", tags=["admin"])


# --- Dashboard data -------------------------------------------------------

@router.get("/overview")
def overview(admin: AdminUser, db: DbSession) -> dict:
    """Headline figures for the super-admin dashboard (the diagram's
    'Dashboard Data': No. of users, billing data, activity)."""
    def count(stmt) -> int:
        return db.scalar(stmt) or 0

    total_users = count(select(func.count()).select_from(User).where(User.deleted_at.is_(None)))
    by_role = dict(db.execute(
        select(User.role, func.count()).where(User.deleted_at.is_(None)).group_by(User.role)
    ).all())
    by_status = dict(db.execute(
        select(User.status, func.count()).where(User.deleted_at.is_(None)).group_by(User.status)
    ).all())

    def role_n(r: UserRole) -> int:
        return by_role.get(r) or by_role.get(r.value) or 0

    def status_n(s: UserStatus) -> int:
        return by_status.get(s) or by_status.get(s.value) or 0

    now = utcnow()
    signups_24h = count(select(func.count()).select_from(User).where(
        User.deleted_at.is_(None), User.created_at >= now - timedelta(days=1)))
    signups_7d = count(select(func.count()).select_from(User).where(
        User.deleted_at.is_(None), User.created_at >= now - timedelta(days=7)))
    signups_30d = count(select(func.count()).select_from(User).where(
        User.deleted_at.is_(None), User.created_at >= now - timedelta(days=30)))

    # Billing / credits — the metering layer that bills recruiters per reveal.
    credit_balance = count(select(func.coalesce(func.sum(CreditAccount.balance), 0)))
    credit_spent = count(select(func.coalesce(func.sum(CreditAccount.lifetime_spent), 0)))
    credit_granted = count(select(func.coalesce(func.sum(CreditAccount.lifetime_granted), 0)))

    return {
        "users": {
            "total": total_users,
            "active": status_n(UserStatus.active),
            "suspended": status_n(UserStatus.suspended),
            "pending_verify": status_n(UserStatus.pending_verify),
            "job_seekers": role_n(UserRole.job_seeker),
            "recruiters": role_n(UserRole.recruiter) + role_n(UserRole.employer),
            "admins": role_n(UserRole.admin),
            "new_24h": signups_24h,
            "new_7d": signups_7d,
            "new_30d": signups_30d,
        },
        "content": {
            "profiles": count(select(func.count()).select_from(Profile)),
            "profiles_listable": count(select(func.count()).select_from(Profile)
                                       .where(Profile.is_listable.is_(True))),
            "jobs": count(select(func.count()).select_from(JobPosting)),
            "jobs_active": count(select(func.count()).select_from(JobPosting)
                                 .where(JobPosting.status == JobStatus.active)),
            "organizations": count(select(func.count()).select_from(Employer)),
            "applications": count(select(func.count()).select_from(Application)),
            "messages": count(select(func.count()).select_from(Message)),
        },
        "billing": {
            "credit_balance": credit_balance,
            "credit_spent": credit_spent,
            "credit_granted": credit_granted,
        },
    }


# --- User management ------------------------------------------------------

def _last_ips(db: DbSession, user_ids: list[str]) -> dict[str, str]:
    """Most recent login IP per user, in one query (for the listed page only)."""
    if not user_ids:
        return {}
    rows = db.execute(
        select(UserSession.user_id, UserSession.ip_address, UserSession.created_at)
        .where(UserSession.user_id.in_(user_ids))
        .order_by(UserSession.created_at.desc())
    ).all()
    out: dict[str, str] = {}
    for uid, ip, _created in rows:
        if uid not in out and ip:            # first row per user = latest session
            out[uid] = ip
    return out


@router.get("/users")
def list_users(
    admin: AdminUser,
    db: DbSession,
    q: Optional[str] = Query(None, description="Search email or name"),
    role: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    """Search, filter and paginate every account on the platform."""
    where = [User.deleted_at.is_(None)]
    if role:
        try:
            where.append(User.role == UserRole(role))
        except ValueError:
            raise HTTPException(400, f"Unknown role '{role}'")
    if status_filter:
        try:
            where.append(User.status == UserStatus(status_filter))
        except ValueError:
            raise HTTPException(400, f"Unknown status '{status_filter}'")
    if q and q.strip():
        term = f"%{q.strip().lower()}%"
        # Match the email, or a linked profile's name.
        name_ids = db.scalars(
            select(Profile.user_id).where(
                func.lower(func.coalesce(Profile.first_name, "")
                           + " " + func.coalesce(Profile.last_name, "")).like(term))
        ).all()
        cond = func.lower(User.email).like(term)
        if name_ids:
            cond = or_(cond, User.user_id.in_(list(name_ids)))
        where.append(cond)

    total = db.scalar(select(func.count()).select_from(User).where(*where)) or 0
    users = db.scalars(
        select(User).where(*where)
        .order_by(User.created_at.desc())
        .limit(limit).offset(offset)
    ).all()

    ids = [u.user_id for u in users]
    ips = _last_ips(db, ids)
    names = dict(db.execute(
        select(Profile.user_id,
               func.coalesce(Profile.first_name, "") + " " + func.coalesce(Profile.last_name, ""))
        .where(Profile.user_id.in_(ids))
    ).all()) if ids else {}
    balances = dict(db.execute(
        select(CreditAccount.user_id, CreditAccount.balance)
        .where(CreditAccount.user_id.in_(ids))
    ).all()) if ids else {}

    def role_val(u: User) -> str:
        return u.role.value if hasattr(u.role, "value") else str(u.role)

    def status_val(u: User) -> str:
        return u.status.value if hasattr(u.status, "value") else str(u.status)

    rows = [{
        "user_id": u.user_id,
        "email": u.email,
        "name": (names.get(u.user_id) or "").strip() or None,
        "role": role_val(u),
        "status": status_val(u),
        "email_verified": u.email_verified_at is not None,
        "credit_balance": balances.get(u.user_id),
        "last_login_at": u.last_login_at,
        "last_ip": ips.get(u.user_id),
        "created_at": u.created_at,
        "is_self": u.user_id == admin.user_id,
    } for u in users]

    return {"users": rows, "total": total, "limit": limit, "offset": offset}


class UserPatch(BaseModel):
    status: Optional[str] = None
    role: Optional[str] = None


def _active_admin_count(db: DbSession, exclude_id: str) -> int:
    return db.scalar(
        select(func.count()).select_from(User).where(
            User.deleted_at.is_(None),
            User.status == UserStatus.active,
            User.role == UserRole.admin,
            User.user_id != exclude_id,
        )
    ) or 0


@router.patch("/users/{user_id}")
def update_user(user_id: str, body: UserPatch, admin: AdminUser, db: DbSession) -> dict:
    """Suspend, reactivate, soft-delete, or change a user's role.

    Guards: an admin can't change their own access, and the last remaining
    active admin can't be demoted or suspended — otherwise the platform could
    be locked out of its own console.
    """
    user = db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise HTTPException(404, "User not found")
    if user.user_id == admin.user_id:
        raise HTTPException(400, "You can't change your own admin account here.")

    changes: dict = {}

    if body.role is not None:
        try:
            new_role = UserRole(body.role)
        except ValueError:
            raise HTTPException(400, f"Unknown role '{body.role}'")
        if user.role == UserRole.admin and new_role != UserRole.admin \
                and _active_admin_count(db, user.user_id) == 0:
            raise HTTPException(400, "Can't demote the last active admin.")
        changes["role"] = new_role.value
        user.role = new_role

    if body.status is not None:
        try:
            new_status = UserStatus(body.status)
        except ValueError:
            raise HTTPException(400, f"Unknown status '{body.status}'")
        if user.role == UserRole.admin and new_status != UserStatus.active \
                and _active_admin_count(db, user.user_id) == 0:
            raise HTTPException(400, "Can't suspend the last active admin.")
        changes["status"] = new_status.value
        user.status = new_status
        if new_status == UserStatus.deleted:
            user.deleted_at = utcnow()

    if not changes:
        raise HTTPException(400, "Nothing to update.")

    db.add(AuditLog(
        actor_user_id=admin.user_id,
        action="admin.user_update",
        entity_type="user",
        entity_id=user.user_id,
        meta=changes,
    ))
    db.commit()
    db.refresh(user)
    return {
        "user_id": user.user_id,
        "role": user.role.value if hasattr(user.role, "value") else str(user.role),
        "status": user.status.value if hasattr(user.status, "value") else str(user.status),
    }


# --- IP tracking / login activity -----------------------------------------

def _short_ua(ua: Optional[str]) -> str:
    """A compact 'Browser on OS' label from a User-Agent string."""
    if not ua:
        return "Unknown device"
    u = ua.lower()
    browser = ("Edge" if "edg" in u else "Chrome" if "chrome" in u and "edg" not in u
               else "Firefox" if "firefox" in u else "Safari" if "safari" in u and "chrome" not in u
               else "App")
    osname = ("Windows" if "windows" in u else "macOS" if "mac os" in u or "macintosh" in u
              else "Android" if "android" in u else "iOS" if "iphone" in u or "ipad" in u
              else "Linux" if "linux" in u else "")
    return f"{browser} on {osname}" if osname else browser


@router.get("/logins")
def recent_logins(
    admin: AdminUser,
    db: DbSession,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """Recent logins across all users, with the visitor IP each came from
    ('Admin sees visitor IPs, logged for all users')."""
    total = db.scalar(select(func.count()).select_from(UserSession)) or 0
    sessions = db.scalars(
        select(UserSession).order_by(UserSession.created_at.desc())
        .limit(limit).offset(offset)
    ).all()
    uids = [s.user_id for s in sessions]
    emails = dict(db.execute(
        select(User.user_id, User.email).where(User.user_id.in_(uids))
    ).all()) if uids else {}
    names = dict(db.execute(
        select(Profile.user_id,
               func.coalesce(Profile.first_name, "") + " " + func.coalesce(Profile.last_name, ""))
        .where(Profile.user_id.in_(uids))
    ).all()) if uids else {}

    now = utcnow()
    rows = []
    for s in sessions:
        ua = (s.device_info or {}).get("user_agent") if isinstance(s.device_info, dict) else None
        active = s.revoked_at is None and s.expires_at is not None and s.expires_at > now
        rows.append({
            "email": emails.get(s.user_id),
            "name": (names.get(s.user_id) or "").strip() or None,
            "ip": s.ip_address,
            "device": _short_ua(ua),
            "created_at": s.created_at,
            "active": active,
        })
    return {"logins": rows, "total": total, "limit": limit, "offset": offset}


# --- Organizations / vendors ----------------------------------------------

@router.get("/organizations")
def list_organizations(
    admin: AdminUser,
    db: DbSession,
    q: Optional[str] = Query(None),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    """Agencies and employers on the board ('Switch Users & Vendors')."""
    where = []
    if q and q.strip():
        where.append(func.lower(Employer.org_name).like(f"%{q.strip().lower()}%"))

    total = db.scalar(select(func.count()).select_from(Employer).where(*where)) or 0
    orgs = db.scalars(
        select(Employer).where(*where)
        .order_by(Employer.created_at.desc())
        .limit(limit).offset(offset)
    ).all()

    ids = [o.employer_id for o in orgs]
    members = dict(db.execute(
        select(EmployerMember.employer_id, func.count())
        .where(EmployerMember.employer_id.in_(ids))
        .group_by(EmployerMember.employer_id)
    ).all()) if ids else {}
    jobs = dict(db.execute(
        select(JobPosting.employer_id, func.count())
        .where(JobPosting.employer_id.in_(ids))
        .group_by(JobPosting.employer_id)
    ).all()) if ids else {}
    active_jobs = dict(db.execute(
        select(JobPosting.employer_id, func.count())
        .where(JobPosting.employer_id.in_(ids), JobPosting.status == JobStatus.active)
        .group_by(JobPosting.employer_id)
    ).all()) if ids else {}
    owner_emails = dict(db.execute(
        select(User.user_id, User.email)
        .where(User.user_id.in_([o.owner_user_id for o in orgs]))
    ).all()) if orgs else {}

    rows = [{
        "employer_id": o.employer_id,
        "org_name": o.org_name,
        "org_type": o.org_type,
        "city": o.city,
        "state_code": o.state_code,
        "is_verified": o.is_verified,
        "owner_email": owner_emails.get(o.owner_user_id),
        "members": (members.get(o.employer_id) or 0) + 1,   # +1 for the owner
        "jobs": jobs.get(o.employer_id) or 0,
        "jobs_active": active_jobs.get(o.employer_id) or 0,
        "created_at": o.created_at,
    } for o in orgs]

    return {"organizations": rows, "total": total, "limit": limit, "offset": offset}
