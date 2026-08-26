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
from sqlalchemy import delete, func, or_, select, update

from ..database import utcnow
from ..deps import AdminUser, DbSession
from ..models import (
    Application,
    ApplicationEvent,
    AuditLog,
    CreditAccount,
    CreditTransaction,
    Employer,
    EmployerMember,
    JobPosting,
    Message,
    Offer,
    Profile,
    SavedJob,
    Session as UserSession,
    TeamInvite,
    User,
)
from ..models.enums import JobStatus, UserRole, UserStatus
from ..services import credits as credits_service

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

    recent = db.scalars(
        select(User).where(User.deleted_at.is_(None))
        .order_by(User.created_at.desc()).limit(6)
    ).all()
    recent_signups = [{
        "email": u.email,
        "role": u.role.value if hasattr(u.role, "value") else str(u.role),
        "created_at": u.created_at,
    } for u in recent]

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
            "jobs_featured": count(select(func.count()).select_from(JobPosting)
                                   .where(JobPosting.is_featured.is_(True))),
            "organizations": count(select(func.count()).select_from(Employer)),
            "organizations_verified": count(select(func.count()).select_from(Employer)
                                            .where(Employer.is_verified.is_(True))),
            "applications": count(select(func.count()).select_from(Application)),
            "messages": count(select(func.count()).select_from(Message)),
        },
        "billing": {
            "credit_balance": credit_balance,
            "credit_spent": credit_spent,
            "credit_granted": credit_granted,
        },
        "recent_signups": recent_signups,
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


# --- User detail + credit adjustment --------------------------------------

def _org_memberships(db: DbSession, user_id: str) -> list[dict]:
    """Every organization a user belongs to (as owner or member), with role."""
    out: list[dict] = []
    owned = db.scalars(select(Employer).where(Employer.owner_user_id == user_id)).all()
    for e in owned:
        out.append({"employer_id": e.employer_id, "org_name": e.org_name, "role": "owner"})
    seen = {e.employer_id for e in owned}
    rows = db.execute(
        select(EmployerMember.employer_id, EmployerMember.member_role, Employer.org_name)
        .join(Employer, Employer.employer_id == EmployerMember.employer_id)
        .where(EmployerMember.user_id == user_id)
    ).all()
    for emp_id, role, name in rows:
        if emp_id not in seen:
            out.append({"employer_id": emp_id, "org_name": name, "role": role or "recruiter"})
    return out


@router.get("/users/{user_id}")
def user_detail(user_id: str, admin: AdminUser, db: DbSession) -> dict:
    """Full record for one account: profile, role/status, credits, org
    memberships and recent logins - the drill-down behind the users table."""
    user = db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise HTTPException(404, "User not found")
    profile = db.scalar(select(Profile).where(Profile.user_id == user_id))
    acct = db.scalar(select(CreditAccount).where(CreditAccount.user_id == user_id))
    ips = _last_ips(db, [user_id])
    sessions = db.scalars(
        select(UserSession).where(UserSession.user_id == user_id)
        .order_by(UserSession.created_at.desc()).limit(5)
    ).all()
    now = utcnow()
    return {
        "user_id": user.user_id,
        "email": user.email,
        "name": (f"{profile.first_name} {profile.last_name}".strip() if profile else None),
        "role": user.role.value if hasattr(user.role, "value") else str(user.role),
        "status": user.status.value if hasattr(user.status, "value") else str(user.status),
        "email_verified": user.email_verified_at is not None,
        "last_login_at": user.last_login_at,
        "last_ip": ips.get(user_id),
        "created_at": user.created_at,
        "is_self": user.user_id == admin.user_id,
        "credits": {
            "balance": acct.balance if acct else 0,
            "lifetime_granted": acct.lifetime_granted if acct else 0,
            "lifetime_spent": acct.lifetime_spent if acct else 0,
        },
        "organizations": _org_memberships(db, user_id),
        "recent_logins": [{
            "ip": s.ip_address,
            "created_at": s.created_at,
            "active": s.revoked_at is None and s.expires_at is not None and s.expires_at > now,
        } for s in sessions],
    }


class CreditAdjust(BaseModel):
    amount: int          # positive to grant, negative to deduct
    note: Optional[str] = None


@router.post("/users/{user_id}/credits")
def adjust_user_credits(user_id: str, body: CreditAdjust, admin: AdminUser, db: DbSession) -> dict:
    """Grant (amount>0) or deduct (amount<0) reveal credits for a user. Deductions
    are floored at zero. Every change lands in the credit ledger."""
    user = db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise HTTPException(404, "User not found")
    if not body.amount:
        raise HTTPException(400, "Amount must be non-zero.")
    res = credits_service.admin_adjust(db, user_id, body.amount, note=body.note, by=admin.email)
    db.add(AuditLog(actor_user_id=admin.user_id, action="admin.credit_adjust",
                    entity_type="user", entity_id=user_id,
                    meta={"amount": body.amount, "balance": res["balance"]}))
    db.commit()
    return res


@router.post("/users/{user_id}/verify")
def verify_user_email(user_id: str, admin: AdminUser, db: DbSession) -> dict:
    """Manually mark a user's email verified and activate a pending account."""
    user = db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise HTTPException(404, "User not found")
    if user.email_verified_at is None:
        user.email_verified_at = utcnow()
    if user.status == UserStatus.pending_verify:
        user.status = UserStatus.active
    db.add(AuditLog(actor_user_id=admin.user_id, action="admin.verify_email",
                    entity_type="user", entity_id=user_id, meta={}))
    db.commit()
    return {"email_verified": True, "status": user.status.value
            if hasattr(user.status, "value") else str(user.status)}


# --- Organization CRUD + members ------------------------------------------

_ORG_MEMBER_ROLES = {"admin", "manager", "recruiter"}


def _require_org(db: DbSession, employer_id: str) -> Employer:
    org = db.get(Employer, employer_id)
    if org is None:
        raise HTTPException(404, "Organization not found")
    return org


class OrgCreate(BaseModel):
    org_name: str
    org_type: Optional[str] = None
    owner_email: Optional[str] = None   # attach an existing user as owner; else the admin
    city: Optional[str] = None
    state_code: Optional[str] = None
    is_verified: bool = True


@router.post("/organizations", status_code=201)
def create_organization(body: OrgCreate, admin: AdminUser, db: DbSession) -> dict:
    """Create a new organization. If owner_email is given it must be an existing
    user, who becomes the owner (and is promoted to recruiter); otherwise the
    admin owns it."""
    name = body.org_name.strip()
    if not name:
        raise HTTPException(400, "Organization name is required.")
    owner = admin
    if body.owner_email:
        owner = db.scalar(select(User).where(User.email == body.owner_email.strip().lower()))
        if owner is None:
            raise HTTPException(404, f"No user with email {body.owner_email}")
        if owner.role == UserRole.job_seeker:
            owner.role = UserRole.recruiter
    org = Employer(
        owner_user_id=owner.user_id, org_name=name[:300],
        org_type=(body.org_type or None), city=(body.city or None),
        state_code=(body.state_code[:2].upper() if body.state_code else None),
        is_verified=body.is_verified,
    )
    db.add(org)
    db.flush()
    db.add(AuditLog(actor_user_id=admin.user_id, action="admin.org_create",
                    entity_type="employer", entity_id=org.employer_id,
                    meta={"org_name": name, "owner": owner.email}))
    db.commit()
    db.refresh(org)
    return {"employer_id": org.employer_id, "org_name": org.org_name}


@router.get("/organizations/{employer_id}")
def organization_detail(employer_id: str, admin: AdminUser, db: DbSession) -> dict:
    """One organization with its owner, members (roles), job counts and the
    owner's credit balance - the drill-down behind the organizations table."""
    org = _require_org(db, employer_id)
    owner = db.get(User, org.owner_user_id)
    owner_profile = db.scalar(select(Profile).where(Profile.user_id == org.owner_user_id))
    owner_acct = db.scalar(select(CreditAccount).where(CreditAccount.user_id == org.owner_user_id))

    members = [{
        "user_id": org.owner_user_id,
        "email": owner.email if owner else None,
        "name": (f"{owner_profile.first_name} {owner_profile.last_name}".strip()
                 if owner_profile else None),
        "role": "owner",
        "is_owner": True,
    }]
    rows = db.execute(
        select(EmployerMember.user_id, EmployerMember.member_role, User.email)
        .join(User, User.user_id == EmployerMember.user_id)
        .where(EmployerMember.employer_id == employer_id)
    ).all()
    mem_ids = [uid for uid, _, _ in rows]
    names = dict(db.execute(
        select(Profile.user_id,
               func.coalesce(Profile.first_name, "") + " " + func.coalesce(Profile.last_name, ""))
        .where(Profile.user_id.in_(mem_ids))
    ).all()) if mem_ids else {}
    for uid, role, email in rows:
        members.append({"user_id": uid, "email": email,
                        "name": (names.get(uid) or "").strip() or None,
                        "role": role or "recruiter", "is_owner": False})

    jobs = db.scalar(select(func.count()).select_from(JobPosting)
                     .where(JobPosting.employer_id == employer_id)) or 0
    jobs_active = db.scalar(select(func.count()).select_from(JobPosting)
                            .where(JobPosting.employer_id == employer_id,
                                   JobPosting.status == JobStatus.active)) or 0
    return {
        "employer_id": org.employer_id,
        "org_name": org.org_name,
        "org_type": org.org_type,
        "city": org.city,
        "state_code": org.state_code,
        "is_verified": org.is_verified,
        "subscription_tier": (org.subscription_tier.value
                              if hasattr(org.subscription_tier, "value")
                              else str(org.subscription_tier)),
        "owner_email": owner.email if owner else None,
        "owner_user_id": org.owner_user_id,
        "owner_credits": owner_acct.balance if owner_acct else 0,
        "jobs": jobs,
        "jobs_active": jobs_active,
        "members": members,
        "created_at": org.created_at,
    }


class OrgPatch(BaseModel):
    org_name: Optional[str] = None
    org_type: Optional[str] = None
    is_verified: Optional[bool] = None
    subscription_tier: Optional[str] = None


@router.patch("/organizations/{employer_id}")
def update_organization(employer_id: str, body: OrgPatch, admin: AdminUser, db: DbSession) -> dict:
    org = _require_org(db, employer_id)
    changes: dict = {}
    if body.org_name is not None and body.org_name.strip():
        org.org_name = body.org_name.strip()[:300]
        changes["org_name"] = org.org_name
    if body.org_type is not None:
        org.org_type = body.org_type or None
        changes["org_type"] = org.org_type
    if body.is_verified is not None:
        org.is_verified = body.is_verified
        changes["is_verified"] = body.is_verified
    if body.subscription_tier is not None:
        from ..models.enums import SubscriptionTier
        try:
            org.subscription_tier = SubscriptionTier(body.subscription_tier)
        except ValueError:
            raise HTTPException(400, f"Unknown tier '{body.subscription_tier}'")
        changes["subscription_tier"] = body.subscription_tier
    if not changes:
        raise HTTPException(400, "Nothing to update.")
    db.add(AuditLog(actor_user_id=admin.user_id, action="admin.org_update",
                    entity_type="employer", entity_id=employer_id, meta=changes))
    db.commit()
    return {"employer_id": employer_id, **changes}


class MemberAdd(BaseModel):
    email: str
    role: str = "recruiter"


@router.post("/organizations/{employer_id}/members", status_code=201)
def add_org_member(employer_id: str, body: MemberAdd, admin: AdminUser, db: DbSession) -> dict:
    """Add an existing user to an organization with a role (recruiter/admin),
    promoting them to a recruiter account so they can use the workspace."""
    org = _require_org(db, employer_id)
    role = body.role.strip().lower()
    if role not in _ORG_MEMBER_ROLES:
        raise HTTPException(400, f"Role must be one of {', '.join(sorted(_ORG_MEMBER_ROLES))}")
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if user is None:
        raise HTTPException(404, f"No user with email {body.email}")
    if user.user_id == org.owner_user_id:
        raise HTTPException(409, "That user already owns this organization.")
    existing = db.scalar(select(EmployerMember).where(
        EmployerMember.employer_id == employer_id, EmployerMember.user_id == user.user_id))
    if existing:
        raise HTTPException(409, "That user is already a member.")
    db.add(EmployerMember(employer_id=employer_id, user_id=user.user_id, member_role=role))
    if user.role == UserRole.job_seeker:
        user.role = UserRole.recruiter
    db.add(AuditLog(actor_user_id=admin.user_id, action="admin.member_add",
                    entity_type="employer", entity_id=employer_id,
                    meta={"user": user.email, "role": role}))
    db.commit()
    return {"user_id": user.user_id, "email": user.email, "role": role}


class MemberRole(BaseModel):
    role: str


@router.patch("/organizations/{employer_id}/members/{user_id}")
def set_org_member_role(employer_id: str, user_id: str, body: MemberRole,
                        admin: AdminUser, db: DbSession) -> dict:
    """Change a member's org role - e.g. promote a recruiter to organization admin."""
    role = body.role.strip().lower()
    if role not in _ORG_MEMBER_ROLES:
        raise HTTPException(400, f"Role must be one of {', '.join(sorted(_ORG_MEMBER_ROLES))}")
    member = db.scalar(select(EmployerMember).where(
        EmployerMember.employer_id == employer_id, EmployerMember.user_id == user_id))
    if member is None:
        raise HTTPException(404, "Not a member of this organization.")
    member.member_role = role
    db.add(AuditLog(actor_user_id=admin.user_id, action="admin.member_role",
                    entity_type="employer", entity_id=employer_id,
                    meta={"user_id": user_id, "role": role}))
    db.commit()
    return {"user_id": user_id, "role": role}


@router.delete("/organizations/{employer_id}/members/{user_id}", status_code=204)
def remove_org_member(employer_id: str, user_id: str, admin: AdminUser, db: DbSession):
    org = _require_org(db, employer_id)
    if user_id == org.owner_user_id:
        raise HTTPException(400, "Can't remove the organization owner.")
    db.execute(delete(EmployerMember).where(
        EmployerMember.employer_id == employer_id, EmployerMember.user_id == user_id))
    db.add(AuditLog(actor_user_id=admin.user_id, action="admin.member_remove",
                    entity_type="employer", entity_id=employer_id, meta={"user_id": user_id}))
    db.commit()


@router.post("/organizations/{employer_id}/credits")
def grant_org_credits(employer_id: str, body: CreditAdjust, admin: AdminUser, db: DbSession) -> dict:
    """Assign reveal credits to an organization - applied to the owner's account,
    which is the seat the org sources against."""
    org = _require_org(db, employer_id)
    if not body.amount:
        raise HTTPException(400, "Amount must be non-zero.")
    res = credits_service.admin_adjust(
        db, org.owner_user_id, body.amount,
        note=body.note or f"Org grant: {org.org_name}", by=admin.email)
    db.add(AuditLog(actor_user_id=admin.user_id, action="admin.org_credit",
                    entity_type="employer", entity_id=employer_id,
                    meta={"amount": body.amount, "balance": res["balance"]}))
    db.commit()
    return res


# --- Job moderation -------------------------------------------------------

@router.get("/jobs")
def list_jobs_admin(
    admin: AdminUser,
    db: DbSession,
    q: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    employer_id: Optional[str] = Query(None),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    """Every job across all organizations, with moderation-relevant flags."""
    where = []
    if status_filter:
        try:
            where.append(JobPosting.status == JobStatus(status_filter))
        except ValueError:
            raise HTTPException(400, f"Unknown status '{status_filter}'")
    if employer_id:
        where.append(JobPosting.employer_id == employer_id)
    if q and q.strip():
        where.append(JobPosting.search_text.like(f"%{q.strip().lower()}%"))

    total = db.scalar(select(func.count()).select_from(JobPosting).where(*where)) or 0
    jobs = db.scalars(
        select(JobPosting).where(*where)
        .order_by(JobPosting.is_featured.desc(), JobPosting.created_at.desc())
        .limit(limit).offset(offset)
    ).all()
    emp_ids = list({j.employer_id for j in jobs})
    org_names = dict(db.execute(
        select(Employer.employer_id, Employer.org_name).where(Employer.employer_id.in_(emp_ids))
    ).all()) if emp_ids else {}
    rows = [{
        "job_id": j.job_id,
        "title": j.title,
        "org_name": org_names.get(j.employer_id),
        "employer_id": j.employer_id,
        "specialty": j.specialty,
        "location": ", ".join(x for x in (j.city, j.state_code) if x) or None,
        "status": j.status.value if hasattr(j.status, "value") else str(j.status),
        "is_featured": j.is_featured,
        "is_urgent": j.is_urgent,
        "applications": j.application_count,
        "source": j.external_source,
        "created_at": j.created_at,
    } for j in jobs]
    return {"jobs": rows, "total": total, "limit": limit, "offset": offset}


class JobModerate(BaseModel):
    is_featured: Optional[bool] = None
    is_urgent: Optional[bool] = None
    status: Optional[str] = None


@router.patch("/jobs/{job_id}")
def moderate_job(job_id: str, body: JobModerate, admin: AdminUser, db: DbSession) -> dict:
    job = db.get(JobPosting, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    changes: dict = {}
    if body.is_featured is not None:
        job.is_featured = body.is_featured
        changes["is_featured"] = body.is_featured
    if body.is_urgent is not None:
        job.is_urgent = body.is_urgent
        changes["is_urgent"] = body.is_urgent
    if body.status is not None:
        try:
            job.status = JobStatus(body.status)
        except ValueError:
            raise HTTPException(400, f"Unknown status '{body.status}'")
        changes["status"] = body.status
    if not changes:
        raise HTTPException(400, "Nothing to update.")
    db.add(AuditLog(actor_user_id=admin.user_id, action="admin.job_moderate",
                    entity_type="job", entity_id=job_id, meta=changes))
    db.commit()
    return {"job_id": job_id, **changes}


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job_admin(job_id: str, admin: AdminUser, db: DbSession):
    """Permanently delete a job and its dependent rows (applications, events,
    saved jobs, offers)."""
    job = db.get(JobPosting, job_id)
    if job is None:
        return
    app_ids = select(Application.application_id).where(Application.job_id == job_id)
    db.execute(delete(ApplicationEvent).where(ApplicationEvent.application_id.in_(app_ids)))
    db.execute(delete(Application).where(Application.job_id == job_id))
    db.execute(delete(SavedJob).where(SavedJob.job_id == job_id))
    db.execute(delete(Offer).where(Offer.job_id == job_id))
    db.execute(delete(JobPosting).where(JobPosting.job_id == job_id))
    db.add(AuditLog(actor_user_id=admin.user_id, action="admin.job_delete",
                    entity_type="job", entity_id=job_id, meta={"title": job.title}))
    db.commit()


# --- Audit log ------------------------------------------------------------

@router.get("/audit")
def audit_log(admin: AdminUser, db: DbSession,
              limit: int = Query(50, ge=1, le=200),
              offset: int = Query(0, ge=0)) -> dict:
    """Recent admin actions, newest first (who did what, to which entity)."""
    total = db.scalar(select(func.count()).select_from(AuditLog)
                      .where(AuditLog.action.like("admin.%"))) or 0
    logs = db.scalars(
        select(AuditLog).where(AuditLog.action.like("admin.%"))
        .order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
    ).all()
    actor_ids = list({l.actor_user_id for l in logs if l.actor_user_id})
    actors = dict(db.execute(
        select(User.user_id, User.email).where(User.user_id.in_(actor_ids))
    ).all()) if actor_ids else {}
    rows = [{
        "action": l.action,
        "actor": actors.get(l.actor_user_id) or "system",
        "entity_type": l.entity_type,
        "entity_id": l.entity_id,
        "meta": l.meta,
        "created_at": l.created_at,
    } for l in logs]
    return {"logs": rows, "total": total, "limit": limit, "offset": offset}
