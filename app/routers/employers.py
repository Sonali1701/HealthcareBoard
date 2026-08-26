"""Employer organisation endpoints."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select, update

from datetime import timedelta

from ..config import settings
from ..database import utcnow
from ..deps import CurrentUser, DbSession
from ..models import (
    Application,
    Employer,
    EmployerMember,
    JobPosting,
    Notification,
    Profile,
    TeamInvite,
    User,
)
from ..models.enums import ApplicationStatus, NotificationType
from ..schemas.common import Page
from ..schemas.job import EmployerCreate, EmployerOut, EmployerUpdate
from ..security import generate_opaque_token, sha256
from ..services import org_roles
from ..services.email import send_team_invite

router = APIRouter(prefix="/api/employers", tags=["employers"])


class MemberInvite(BaseModel):
    email: EmailStr
    member_role: str = "recruiter"


def _require_cap(db: DbSession, employer: Employer, user: CurrentUser, capability: str) -> str:
    """Enforce an org-level capability; returns the acting user's org role."""
    role = org_roles.role_of(db, employer, user)
    if role is None:
        raise HTTPException(status_code=403, detail="Not a member of this organisation")
    if not org_roles.can(role, capability):
        raise HTTPException(status_code=403,
                            detail="Your organization role doesn't allow that action")
    return role


def _guard_role_assignment(actor_role: str, target_role: str) -> None:
    """A manager can add/manage plain members, but only owners and admins may
    grant the elevated admin or manager roles."""
    if org_roles.rank(target_role) >= org_roles.rank("manager") \
            and not org_roles.can(actor_role, "manage_roles"):
        raise HTTPException(
            status_code=403,
            detail="Only owners and admins can assign the admin or manager role")


@router.get("/me/dashboard")
def my_employer_dashboard(user: CurrentUser, db: DbSession):
    """Everything the Employer Portal needs: org, KPIs, jobs, recent applicants."""
    emp = db.scalar(select(Employer).where(Employer.owner_user_id == user.user_id))
    if not emp:
        member = db.scalar(select(EmployerMember).where(EmployerMember.user_id == user.user_id))
        emp = db.get(Employer, member.employer_id) if member else None
    if not emp:
        return {"employer": None, "kpis": {}, "jobs": [], "applicants": []}

    jobs = db.scalars(select(JobPosting).where(JobPosting.employer_id == emp.employer_id)
                      .order_by(JobPosting.created_at.desc())).all()
    job_ids = [j.job_id for j in jobs]
    jobmap = {j.job_id: j for j in jobs}
    apps = db.scalars(select(Application).where(Application.job_id.in_(job_ids))
                      .order_by(Application.applied_at.desc()).limit(15)).all() if job_ids else []
    profs = {p.profile_id: p for p in db.scalars(
        select(Profile).where(Profile.profile_id.in_([a.profile_id for a in apps])))} if apps else {}

    def _count(status):
        if not job_ids:
            return 0
        return db.scalar(select(func.count()).select_from(Application).where(
            Application.job_id.in_(job_ids), Application.status == status)) or 0

    applicants = []
    for a in apps:
        p = profs.get(a.profile_id)
        applicants.append({
            "application_id": a.application_id, "profile_id": a.profile_id,
            "name": f"{p.first_name} {p.last_name}" if p else "—",
            "specialty": p.specialty if p else None,
            "years": p.years_experience if p else None,
            "location": ", ".join(x for x in [p.city, p.state_code] if x) if p else None,
            "completion": p.completion_score if p else 0,
            "job_title": jobmap[a.job_id].title if a.job_id in jobmap else "",
            "status": a.status.value,
        })
    return {
        "employer": {"employer_id": emp.employer_id, "org_name": emp.org_name,
                     "org_type": emp.org_type, "city": emp.city,
                     "state_code": emp.state_code, "website_url": emp.website_url,
                     "description": emp.description, "is_verified": emp.is_verified,
                     "rating_avg": float(emp.rating_avg or 0)},
        "kpis": {"jobs": len(jobs),
                 "applications": (db.scalar(select(func.count()).select_from(Application)
                                  .where(Application.job_id.in_(job_ids))) or 0) if job_ids else 0,
                 "interviews": _count(ApplicationStatus.interview),
                 "offers": _count(ApplicationStatus.offer),
                 "hired": _count(ApplicationStatus.hired)},
        "jobs": [{"job_id": j.job_id, "title": j.title, "job_type": j.job_type.value,
                  "specialty": j.specialty, "profession_type": j.profession_type,
                  "city": j.city, "state_code": j.state_code,
                  "pay_rate_max": float(j.pay_rate_max) if j.pay_rate_max else None,
                  "pay_unit": j.pay_unit, "application_count": j.application_count,
                  "view_count": j.view_count, "status": j.status.value,
                  "is_urgent": j.is_urgent} for j in jobs],
        "applicants": applicants,
    }


def _require_member(db: DbSession, employer: Employer, user: CurrentUser) -> None:
    if employer.owner_user_id == user.user_id or user.role.value == "admin":
        return
    member = db.scalar(
        select(EmployerMember).where(
            EmployerMember.employer_id == employer.employer_id,
            EmployerMember.user_id == user.user_id,
        )
    )
    if not member:
        raise HTTPException(status_code=403, detail="Not a member of this organisation")


@router.get("", response_model=Page[EmployerOut])
def list_employers(
    db: DbSession,
    q: Optional[str] = None,
    state_code: Optional[str] = None,
    org_type: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    stmt = select(Employer)
    if q:
        stmt = stmt.where(Employer.org_name.ilike(f"%{q}%"))
    if state_code:
        stmt = stmt.where(Employer.state_code == state_code.upper())
    if org_type:
        stmt = stmt.where(Employer.org_type == org_type)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.limit(limit).offset(offset)).all()
    return Page(items=rows, total=total, limit=limit, offset=offset)


@router.post("", response_model=EmployerOut, status_code=status.HTTP_201_CREATED)
def create_employer(body: EmployerCreate, user: CurrentUser, db: DbSession):
    employer = Employer(owner_user_id=user.user_id, **body.model_dump())
    db.add(employer)
    db.flush()
    db.add(EmployerMember(
        employer_id=employer.employer_id, user_id=user.user_id, member_role="owner"
    ))
    db.commit()
    db.refresh(employer)
    return employer


@router.get("/{employer_id}", response_model=EmployerOut)
def get_employer(employer_id: str, db: DbSession):
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(status_code=404, detail="Employer not found")
    return employer


@router.patch("/{employer_id}", response_model=EmployerOut)
def update_employer(employer_id: str, body: EmployerUpdate, user: CurrentUser, db: DbSession):
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(status_code=404, detail="Employer not found")
    _require_cap(db, employer, user, "settings")   # owner / admin only
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(employer, field, value)
    db.commit()
    db.refresh(employer)
    return employer


# --- Team members ---------------------------------------------------------
# Shared pools, team submissions and "everyone at my agency" visibility all key
# off EmployerMember rows, but until now the only row ever created was the
# owner's own — so a team could never exceed one person. These make it real.

def _require_owner(employer: Employer, user: CurrentUser) -> None:
    if employer.owner_user_id != user.user_id and user.role.value != "admin":
        raise HTTPException(status_code=403,
                            detail="Only the organisation owner can manage the team")


@router.get("/{employer_id}/members")
def list_members(employer_id: str, user: CurrentUser, db: DbSession):
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(status_code=404, detail="Employer not found")
    _require_member(db, employer, user)
    members = db.scalars(select(EmployerMember).where(
        EmployerMember.employer_id == employer_id)).all()
    users = {u.user_id: u for u in db.scalars(
        select(User).where(User.user_id.in_([m.user_id for m in members])))} if members else {}
    profs = {p.user_id: p for p in db.scalars(
        select(Profile).where(Profile.user_id.in_([m.user_id for m in members])))} if members else {}

    def _name(uid: str) -> Optional[str]:
        p = profs.get(uid)
        return f"{p.first_name} {p.last_name}".strip() if p else None

    my_role = org_roles.role_of(db, employer, user)
    perms = org_roles.permissions(my_role)
    items = []
    for m in members:
        u = users.get(m.user_id)
        is_owner = m.user_id == employer.owner_user_id
        role = "owner" if is_owner else org_roles.normalize_role(m.member_role)
        items.append({
            "user_id": m.user_id,
            "email": u.email if u else None,
            "name": _name(m.user_id),
            "member_role": role,
            "role_label": org_roles.ROLE_LABELS.get(role, role),
            "is_owner": is_owner,
        })
    items.sort(key=lambda x: (-org_roles.rank(x["member_role"]),
                              (x["name"] or x["email"] or "").lower()))
    return {"items": items,
            # kept for backward-compat with the existing UI; equals manage_members
            "can_manage": bool(perms.get("manage_members")),
            "my_role": my_role,
            "permissions": perms,
            "assignable_roles": ["recruiter", "manager", "admin"],
            "owner_user_id": employer.owner_user_id}


@router.post("/{employer_id}/members", status_code=201)
def invite_member(employer_id: str, body: MemberInvite, user: CurrentUser, db: DbSession):
    """Add an existing HealthBoard user to the organisation by email."""
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(status_code=404, detail="Employer not found")
    actor_role = _require_cap(db, employer, user, "manage_members")
    role = org_roles.normalize_role(body.member_role)
    _guard_role_assignment(actor_role, role)

    invitee = db.scalar(select(User).where(
        func.lower(User.email) == body.email.strip().lower()))
    if not invitee or invitee.deleted_at is not None:
        raise HTTPException(status_code=404,
                            detail="No HealthBoard account with that email. Ask them to "
                                   "create an account first, then invite them.")
    if invitee.user_id == employer.owner_user_id:
        raise HTTPException(status_code=400, detail="You already own this organisation")
    if db.scalar(select(EmployerMember).where(
            EmployerMember.employer_id == employer_id,
            EmployerMember.user_id == invitee.user_id)):
        raise HTTPException(status_code=409, detail="They are already on your team")

    db.add(EmployerMember(employer_id=employer_id, user_id=invitee.user_id,
                          member_role=role))
    from ..models.enums import UserRole as _UR
    if invitee.role == _UR.job_seeker:
        invitee.role = _UR.recruiter
    db.add(Notification(
        user_id=invitee.user_id, type=NotificationType.system,
        title="Added to a team",
        body=f"You were added to {employer.org_name} on HealthBoard.",
        data={"employer_id": employer_id}))
    db.commit()
    if invitee.email:
        send_team_invite(invitee.email, employer.org_name)
    return {"added": True, "user_id": invitee.user_id, "email": invitee.email}


@router.delete("/{employer_id}/members/{member_user_id}", status_code=204)
def remove_member(employer_id: str, member_user_id: str, user: CurrentUser, db: DbSession):
    employer = db.get(Employer, employer_id)
    if not employer:
        return
    actor_role = _require_cap(db, employer, user, "manage_members")
    if member_user_id == employer.owner_user_id:
        raise HTTPException(status_code=400, detail="The owner cannot be removed")
    m = db.scalar(select(EmployerMember).where(
        EmployerMember.employer_id == employer_id,
        EmployerMember.user_id == member_user_id))
    if m:
        # A manager may remove members at or below their level, not an admin.
        target_role = org_roles.normalize_role(m.member_role)
        if not org_roles.can(actor_role, "manage_roles") \
                and org_roles.rank(target_role) >= org_roles.rank(actor_role):
            raise HTTPException(status_code=403,
                                detail="You can't remove a member at or above your own role")
        db.delete(m)
        db.commit()


class MemberRoleUpdate(BaseModel):
    member_role: str


@router.patch("/{employer_id}/members/{member_user_id}")
def set_member_role(employer_id: str, member_user_id: str, body: MemberRoleUpdate,
                    user: CurrentUser, db: DbSession):
    """Change a member's org role (Member / Manager / Admin). Owner/admin only."""
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(status_code=404, detail="Employer not found")
    actor_role = _require_cap(db, employer, user, "manage_roles")
    if member_user_id == employer.owner_user_id:
        raise HTTPException(status_code=400, detail="The owner's role can't be changed")
    role = org_roles.normalize_role(body.member_role)
    _guard_role_assignment(actor_role, role)
    m = db.scalar(select(EmployerMember).where(
        EmployerMember.employer_id == employer_id,
        EmployerMember.user_id == member_user_id))
    if not m:
        raise HTTPException(status_code=404, detail="Not a member of this organisation")
    m.member_role = role
    db.commit()
    return {"user_id": member_user_id, "member_role": role,
            "role_label": org_roles.ROLE_LABELS.get(role, role)}


@router.get("/{employer_id}/usage")
def org_usage(employer_id: str, user: CurrentUser, db: DbSession):
    """Per-member usage for the org — credits and contacts revealed — so a
    manager/admin can 'track user usage' and see billing at a glance."""
    from ..models import AuditLog, CreditAccount
    from .profiles import RELEASE_ACTION

    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(status_code=404, detail="Employer not found")
    _require_cap(db, employer, user, "analytics")

    members = db.scalars(select(EmployerMember).where(
        EmployerMember.employer_id == employer_id)).all()
    ids = list({employer.owner_user_id, *[m.user_id for m in members]})
    role_by = {m.user_id: org_roles.normalize_role(m.member_role) for m in members}
    role_by[employer.owner_user_id] = "owner"

    users = {u.user_id: u for u in db.scalars(select(User).where(User.user_id.in_(ids)))}
    profs = {p.user_id: p for p in db.scalars(select(Profile).where(Profile.user_id.in_(ids)))}
    accts = {a.user_id: a for a in db.scalars(
        select(CreditAccount).where(CreditAccount.user_id.in_(ids)))}
    reveals = dict(db.execute(
        select(AuditLog.actor_user_id, func.count())
        .where(AuditLog.actor_user_id.in_(ids), AuditLog.action == RELEASE_ACTION)
        .group_by(AuditLog.actor_user_id)).all())

    rows = []
    for uid in ids:
        u = users.get(uid)
        p = profs.get(uid)
        a = accts.get(uid)
        rows.append({
            "user_id": uid,
            "email": u.email if u else None,
            "name": (f"{p.first_name} {p.last_name}".strip() if p else None),
            "role": role_by.get(uid, "recruiter"),
            "role_label": org_roles.ROLE_LABELS.get(role_by.get(uid, "recruiter")),
            "credits": a.balance if a else 0,
            "credits_spent": a.lifetime_spent if a else 0,
            "reveals": reveals.get(uid, 0),
        })
    rows.sort(key=lambda x: (-org_roles.rank(x["role"]), -x["reveals"]))
    totals = {
        "credits": sum(r["credits"] for r in rows),
        "reveals": sum(r["reveals"] for r in rows),
        "members": len(rows),
    }
    return {"members": rows, "totals": totals,
            "can_view_billing": org_roles.can(org_roles.role_of(db, employer, user), "billing")}


# --- Team invitations (invite anyone by email) ----------------------------
# Unlike adding an existing member, an invitation reaches someone who may not
# have an account yet: they receive a link, sign up or sign in, and join.

_INVITE_ROLES = {"admin", "manager", "recruiter"}


class InviteCreate(BaseModel):
    email: EmailStr
    role: str = "recruiter"          # admin | manager | recruiter


class InviteAccept(BaseModel):
    token: str


@router.post("/{employer_id}/invites", status_code=201)
def create_invite(employer_id: str, body: InviteCreate, user: CurrentUser, db: DbSession):
    """Invite someone to the team by email — an account is not required yet."""
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(status_code=404, detail="Employer not found")
    actor_role = _require_cap(db, employer, user, "manage_members")
    role = org_roles.normalize_role(body.role) if body.role in _INVITE_ROLES else "recruiter"
    _guard_role_assignment(actor_role, role)
    email = body.email.strip().lower()

    existing = db.scalar(select(User).where(func.lower(User.email) == email))
    if existing and (existing.user_id == employer.owner_user_id or db.scalar(
            select(EmployerMember).where(EmployerMember.employer_id == employer_id,
                                         EmployerMember.user_id == existing.user_id))):
        raise HTTPException(status_code=409, detail="They are already on your team")

    # Supersede any earlier pending invite for the same person.
    db.execute(update(TeamInvite).where(
        TeamInvite.employer_id == employer_id, TeamInvite.email == email,
        TeamInvite.status == "pending").values(status="revoked"))

    raw = generate_opaque_token()
    inv = TeamInvite(employer_id=employer_id, email=email, role=role,
                     token_hash=sha256(raw), status="pending",
                     invited_by_user_id=user.user_id,
                     expires_at=utcnow() + timedelta(days=14))
    db.add(inv)
    db.commit()
    link = f"{settings.frontend_base_url.rstrip('/')}/?invite={raw}"
    send_team_invite(email, employer.org_name, accept_link=link)
    return {"invite_id": inv.invite_id, "email": email, "role": role, "status": "pending"}


@router.get("/{employer_id}/invites")
def list_invites(employer_id: str, user: CurrentUser, db: DbSession):
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(status_code=404, detail="Employer not found")
    _require_cap(db, employer, user, "manage_members")
    invs = db.scalars(select(TeamInvite).where(
        TeamInvite.employer_id == employer_id, TeamInvite.status == "pending")
        .order_by(TeamInvite.created_at.desc())).all()
    return {"items": [{"invite_id": i.invite_id, "email": i.email, "role": i.role,
                       "created_at": i.created_at, "expires_at": i.expires_at} for i in invs]}


@router.delete("/{employer_id}/invites/{invite_id}", status_code=204)
def revoke_invite(employer_id: str, invite_id: str, user: CurrentUser, db: DbSession):
    employer = db.get(Employer, employer_id)
    if not employer:
        return
    _require_cap(db, employer, user, "manage_members")
    inv = db.get(TeamInvite, invite_id)
    if inv and inv.employer_id == employer_id and inv.status == "pending":
        inv.status = "revoked"
        db.commit()


@router.post("/invites/accept")
def accept_invite(body: InviteAccept, user: CurrentUser, db: DbSession):
    """The signed-in user accepts an invitation and joins the organisation."""
    inv = db.scalar(select(TeamInvite).where(TeamInvite.token_hash == sha256(body.token.strip())))
    if not inv or inv.status != "pending":
        raise HTTPException(status_code=400, detail="This invitation is no longer valid.")
    if inv.expires_at < utcnow():
        inv.status = "revoked"
        db.commit()
        raise HTTPException(status_code=400, detail="This invitation has expired.")
    employer = db.get(Employer, inv.employer_id)
    if not employer:
        raise HTTPException(status_code=404, detail="Organisation not found")

    already = user.user_id == employer.owner_user_id or bool(db.scalar(
        select(EmployerMember).where(EmployerMember.employer_id == inv.employer_id,
                                     EmployerMember.user_id == user.user_id)))
    if not already:
        db.add(EmployerMember(employer_id=inv.employer_id, user_id=user.user_id,
                              member_role=inv.role))
    inv.status = "accepted"
    db.commit()
    return {"joined": True, "org_name": employer.org_name, "already": already}
