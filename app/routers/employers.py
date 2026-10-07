"""Employer organisation endpoints."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, func, select, update

from datetime import timedelta

from ..config import settings
from ..database import utcnow
from ..deps import CurrentUser, DbSession
from ..models import (
    Application,
    AuditLog,
    Employer,
    EmployerMember,
    JobPosting,
    MedhuntMessagingPermission,
    MedhuntSmsSender,
    Notification,
    OrganizationTeam,
    OrganizationTeamMember,
    Profile,
    TeamInvite,
    User,
)
from ..models.enums import ApplicationStatus, NotificationType, UserRole
from ..schemas.common import Page
from ..schemas.job import EmployerCreate, EmployerOut, EmployerUpdate
from ..security import generate_opaque_token, sha256
from ..services import org_roles
from ..services import team_access
from ..services.email import send_team_invite

router = APIRouter(prefix="/api/employers", tags=["employers"])


class MemberInvite(BaseModel):
    email: EmailStr
    member_role: str = "recruiter"
    team_id: Optional[str] = None


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
def my_employer_dashboard(user: CurrentUser, db: DbSession,
                          employer_id: str | None = None):
    """Everything the Employer Portal needs: org, KPIs, jobs, recent applicants."""
    if employer_id:
        emp = db.get(Employer, employer_id)
        if not emp:
            raise HTTPException(status_code=404, detail="Employer not found")
        _require_member(db, emp, user)
    else:
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
                     "rating_avg": float(emp.rating_avg or 0),
                     "quick_sourcer_per_user_limit": int(emp.quick_sourcer_per_user_limit or 10)},
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
    if body.quick_sourcer_per_user_limit is not None:
        db.add(AuditLog(
            actor_user_id=user.user_id,
            action="organization_quick_sourcer_limit_updated",
            entity_type="employer", entity_id=employer_id,
            meta={"quick_sourcer_per_user_limit": body.quick_sourcer_per_user_limit},
        ))
    db.commit()
    db.refresh(employer)
    return employer


# --- Organization teams --------------------------------------------------


class OrganizationTeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class OrganizationTeamUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    status: Optional[str] = Field(default=None, pattern=r"^(active|archived)$")


class OrganizationTeamMemberChange(BaseModel):
    user_id: str = Field(min_length=1, max_length=80)
    team_role: str = Field(default="member", pattern=r"^(manager|member)$")
    status: str = Field(default="active", pattern=r"^(active|paused)$")


class OrganizationTeamMemberBulkChange(BaseModel):
    user_ids: list[str] = Field(min_length=1, max_length=500)
    team_role: str = Field(default="member", pattern=r"^(manager|member)$")
    mode: str = Field(default="move", pattern=r"^(move|add)$")


def _organization_team(db: DbSession, employer_id: str, team_id: str) -> OrganizationTeam:
    team = db.scalar(select(OrganizationTeam).where(
        OrganizationTeam.team_id == team_id,
        OrganizationTeam.employer_id == employer_id,
    ))
    if not team:
        raise HTTPException(404, "Team not found")
    return team


def _require_team_manager(db: DbSession, employer: Employer, team: OrganizationTeam,
                          user: CurrentUser) -> str:
    role = org_roles.role_of(db, employer, user)
    if role in {"owner", "admin"}:
        return role
    managed = team_access.managed_team_ids(db, employer, user)
    if role != "manager" or team.team_id not in managed:
        raise HTTPException(403, "You can manage only teams assigned to you.")
    return role


@router.get("/{employer_id}/teams")
def list_organization_teams(employer_id: str, user: CurrentUser, db: DbSession):
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(404, "Employer not found")
    role = org_roles.role_of(db, employer, user)
    if role is None:
        raise HTTPException(403, "Not a member of this organisation")
    team_ids = team_access.accessible_team_ids(db, employer, user)
    teams = db.scalars(select(OrganizationTeam).where(
        OrganizationTeam.employer_id == employer_id,
        OrganizationTeam.team_id.in_(team_ids),
    ).order_by(OrganizationTeam.name)).all() if team_ids else []
    memberships = db.scalars(select(OrganizationTeamMember).where(
        OrganizationTeamMember.team_id.in_(team_ids),
    )).all() if team_ids else []
    by_team: dict[str, list[OrganizationTeamMember]] = {}
    for membership in memberships:
        by_team.setdefault(membership.team_id, []).append(membership)
    return {
        "items": [{
            "team_id": team.team_id,
            "name": team.name,
            "status": team.status,
            "member_count": len(by_team.get(team.team_id, [])),
            "manager_user_ids": sorted(
                member.user_id for member in by_team.get(team.team_id, [])
                if member.team_role == "manager" and member.status == "active"
            ),
        } for team in teams],
        "can_create": role in {"owner", "admin"},
        "managed_team_ids": sorted(team_access.managed_team_ids(db, employer, user)),
    }


@router.post("/{employer_id}/teams", status_code=201)
def create_organization_team(employer_id: str, body: OrganizationTeamCreate,
                             user: CurrentUser, db: DbSession):
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(404, "Employer not found")
    role = _require_cap(db, employer, user, "manage_roles")
    name = " ".join(body.name.split())
    duplicate = db.scalar(select(OrganizationTeam.team_id).where(
        OrganizationTeam.employer_id == employer_id,
        func.lower(OrganizationTeam.name) == name.lower(),
    ))
    if duplicate:
        raise HTTPException(409, "A team with that name already exists.")
    team = OrganizationTeam(
        employer_id=employer_id, name=name, created_by_user_id=user.user_id,
    )
    db.add(team)
    db.flush()
    db.add(AuditLog(
        actor_user_id=user.user_id, action="organization_team_created",
        entity_type="organization_team", entity_id=team.team_id,
        meta={"employer_id": employer_id, "name": name, "actor_role": role},
    ))
    db.commit()
    return {"team_id": team.team_id, "name": team.name, "status": team.status}


@router.patch("/{employer_id}/teams/{team_id}")
def update_organization_team(employer_id: str, team_id: str,
                             body: OrganizationTeamUpdate,
                             user: CurrentUser, db: DbSession):
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(404, "Employer not found")
    _require_cap(db, employer, user, "manage_roles")
    team = _organization_team(db, employer_id, team_id)
    changes = {}
    if body.name is not None:
        team.name = " ".join(body.name.split())
        changes["name"] = team.name
    if body.status is not None:
        team.status = body.status
        changes["status"] = team.status
    if not changes:
        raise HTTPException(400, "Nothing to update.")
    db.add(AuditLog(
        actor_user_id=user.user_id, action="organization_team_updated",
        entity_type="organization_team", entity_id=team.team_id,
        meta={"employer_id": employer_id, **changes},
    ))
    db.commit()
    return {"team_id": team.team_id, "name": team.name, "status": team.status}


@router.post("/{employer_id}/teams/{team_id}/members", status_code=201)
def add_organization_team_member(employer_id: str, team_id: str,
                                 body: OrganizationTeamMemberChange,
                                 user: CurrentUser, db: DbSession):
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(404, "Employer not found")
    team = _organization_team(db, employer_id, team_id)
    actor_role = _require_team_manager(db, employer, team, user)
    if body.user_id not in team_access.organization_member_ids(db, employer):
        raise HTTPException(404, "User is not an organization member.")
    if body.team_role == "manager" and actor_role not in {"owner", "admin"}:
        raise HTTPException(403, "Only organization admins can assign team managers.")
    if actor_role == "manager":
        target_membership = db.scalar(select(EmployerMember).where(
            EmployerMember.employer_id == employer_id,
            EmployerMember.user_id == body.user_id,
        ))
        if (not target_membership
                or org_roles.normalize_role(target_membership.member_role) != "recruiter"):
            raise HTTPException(403, "Managers can assign only organization members to their teams.")
    membership = db.scalar(select(OrganizationTeamMember).where(
        OrganizationTeamMember.team_id == team_id,
        OrganizationTeamMember.user_id == body.user_id,
    ))
    if membership:
        membership.team_role = body.team_role
        membership.status = body.status
    else:
        membership = OrganizationTeamMember(
            team_id=team_id, user_id=body.user_id,
            team_role=body.team_role, status=body.status,
            created_by_user_id=user.user_id,
        )
        db.add(membership)
    db.add(AuditLog(
        actor_user_id=user.user_id, action="organization_team_member_added",
        entity_type="organization_team", entity_id=team_id,
        meta={"employer_id": employer_id, "user_id": body.user_id,
              "team_role": body.team_role, "status": body.status},
    ))
    db.commit()
    return {"team_id": team_id, "user_id": body.user_id,
            "team_role": membership.team_role, "status": membership.status}


@router.post("/{employer_id}/teams/{team_id}/members/bulk")
def bulk_assign_organization_team_members(
    employer_id: str, team_id: str, body: OrganizationTeamMemberBulkChange,
    user: CurrentUser, db: DbSession,
):
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(404, "Employer not found")
    actor_role = _require_cap(db, employer, user, "manage_roles")
    team = _organization_team(db, employer_id, team_id)
    if team.status != "active":
        raise HTTPException(409, "Choose an active team.")
    user_ids = list(dict.fromkeys(body.user_ids))
    organization_users = team_access.organization_member_ids(db, employer)
    invalid = [user_id for user_id in user_ids if user_id not in organization_users]
    if invalid:
        raise HTTPException(404, "One or more selected users are not organization members.")
    employer_team_ids = list(db.scalars(select(OrganizationTeam.team_id).where(
        OrganizationTeam.employer_id == employer_id,
    )).all())
    if body.mode == "move" and employer_team_ids:
        db.execute(delete(OrganizationTeamMember).where(
            OrganizationTeamMember.team_id.in_(employer_team_ids),
            OrganizationTeamMember.team_id != team_id,
            OrganizationTeamMember.user_id.in_(user_ids),
        ))
    existing = {
        membership.user_id: membership
        for membership in db.scalars(select(OrganizationTeamMember).where(
            OrganizationTeamMember.team_id == team_id,
            OrganizationTeamMember.user_id.in_(user_ids),
        )).all()
    }
    for user_id in user_ids:
        membership = existing.get(user_id)
        if membership:
            membership.team_role = body.team_role
            membership.status = "active"
        else:
            db.add(OrganizationTeamMember(
                team_id=team_id, user_id=user_id, team_role=body.team_role,
                status="active", created_by_user_id=user.user_id,
            ))
    db.add(AuditLog(
        actor_user_id=user.user_id, action="organization_team_members_bulk_assigned",
        entity_type="organization_team", entity_id=team_id,
        meta={"employer_id": employer_id, "user_ids": user_ids,
              "team_role": body.team_role, "mode": body.mode,
              "actor_role": actor_role},
    ))
    db.commit()
    return {"team_id": team_id, "updated": len(user_ids),
            "team_role": body.team_role, "mode": body.mode}


@router.delete("/{employer_id}/teams/{team_id}/members/{member_user_id}", status_code=204)
def remove_organization_team_member(employer_id: str, team_id: str,
                                    member_user_id: str,
                                    user: CurrentUser, db: DbSession):
    employer = db.get(Employer, employer_id)
    if not employer:
        return
    team = _organization_team(db, employer_id, team_id)
    actor_role = _require_team_manager(db, employer, team, user)
    membership = db.scalar(select(OrganizationTeamMember).where(
        OrganizationTeamMember.team_id == team_id,
        OrganizationTeamMember.user_id == member_user_id,
    ))
    if not membership:
        return
    if membership.team_role == "manager" and actor_role not in {"owner", "admin"}:
        raise HTTPException(403, "Managers cannot remove another team manager.")
    db.delete(membership)
    db.add(AuditLog(
        actor_user_id=user.user_id, action="organization_team_member_removed",
        entity_type="organization_team", entity_id=team_id,
        meta={"employer_id": employer_id, "user_id": member_user_id},
    ))
    db.commit()


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
    visible_ids = (team_access.manageable_member_ids(db, employer, user)
                   if perms.get("manage_members")
                   else team_access.scoped_member_ids(db, employer, user))
    visible_team_ids = team_access.accessible_team_ids(db, employer, user)
    team_memberships = db.execute(
        select(OrganizationTeamMember, OrganizationTeam)
        .join(OrganizationTeam, OrganizationTeam.team_id == OrganizationTeamMember.team_id)
        .where(
            OrganizationTeam.employer_id == employer_id,
            OrganizationTeam.team_id.in_(visible_team_ids),
        )
    ).all() if visible_team_ids else []
    teams_by_user: dict[str, list[dict]] = {}
    for membership, team in team_memberships:
        teams_by_user.setdefault(membership.user_id, []).append({
            "team_id": team.team_id,
            "team_name": team.name,
            "team_role": membership.team_role,
            "status": membership.status,
        })
    items = []
    for m in members:
        if m.user_id not in visible_ids:
            continue
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
            "ats_destination": "ceipal" if u and u.medhunt_ceipal_enabled else "nexus",
            "quick_sourcer_limit_override": m.quick_sourcer_limit_override,
            "teams": sorted(teams_by_user.get(m.user_id, []), key=lambda item: item["team_name"].lower()),
            "messaging_status": team_access.messaging_status(db, employer, m.user_id),
            "can_manage_member": team_access.can_manage_member(db, employer, user, m.user_id),
        })
    items.sort(key=lambda x: (-org_roles.rank(x["member_role"]),
                              (x["name"] or x["email"] or "").lower()))
    return {"items": items,
            "organization_quick_sourcer_limit": int(employer.quick_sourcer_per_user_limit or 10),
            # kept for backward-compat with the existing UI; equals manage_members
            "can_manage": bool(perms.get("manage_members")),
            "my_role": my_role,
            "permissions": perms,
            "assignable_roles": ["recruiter", "manager", "admin"],
            "owner_user_id": employer.owner_user_id}


def _team_for_new_member(db: DbSession, employer: Employer, actor: CurrentUser,
                         requested_team_id: str | None) -> OrganizationTeam:
    role = org_roles.role_of(db, employer, actor)
    if requested_team_id:
        team = _organization_team(db, employer.employer_id, requested_team_id)
        if role == "manager" and team.team_id not in team_access.managed_team_ids(db, employer, actor):
            raise HTTPException(403, "You can add members only to teams assigned to you.")
        return team
    if role == "manager":
        managed = team_access.managed_team_ids(db, employer, actor)
        if len(managed) != 1:
            raise HTTPException(422, "Choose which managed team this member should join.")
        return _organization_team(db, employer.employer_id, next(iter(managed)))
    team = db.scalar(select(OrganizationTeam).where(
        OrganizationTeam.employer_id == employer.employer_id,
        OrganizationTeam.status == "active",
    ).order_by(func.lower(OrganizationTeam.name) != "general", OrganizationTeam.name).limit(1))
    if not team:
        raise HTTPException(409, "Create an active organization team before adding members.")
    return team


@router.post("/{employer_id}/members", status_code=201)
def invite_member(employer_id: str, body: MemberInvite, user: CurrentUser, db: DbSession):
    """Add an existing MedHunt user to the organisation by email."""
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(status_code=404, detail="Employer not found")
    actor_role = _require_cap(db, employer, user, "manage_members")
    role = org_roles.normalize_role(body.member_role)
    _guard_role_assignment(actor_role, role)
    team = _team_for_new_member(db, employer, user, body.team_id)

    invitee = db.scalar(select(User).where(
        func.lower(User.email) == body.email.strip().lower()))
    if not invitee or invitee.deleted_at is not None:
        raise HTTPException(status_code=404,
                            detail="No MedHunt account with that email. Ask them to "
                                   "create an account first, then invite them.")
    if invitee.user_id == employer.owner_user_id:
        raise HTTPException(status_code=400, detail="You already own this organisation")
    if db.scalar(select(EmployerMember).where(
            EmployerMember.employer_id == employer_id,
            EmployerMember.user_id == invitee.user_id)):
        raise HTTPException(status_code=409, detail="They are already on your team")

    db.add(EmployerMember(employer_id=employer_id, user_id=invitee.user_id,
                          member_role=role))
    db.add(OrganizationTeamMember(
        team_id=team.team_id,
        user_id=invitee.user_id,
        team_role="manager" if role == "manager" else "member",
        created_by_user_id=user.user_id,
    ))
    from ..models.enums import UserRole as _UR
    if invitee.role == _UR.job_seeker:
        invitee.role = _UR.recruiter
    db.add(Notification(
        user_id=invitee.user_id, type=NotificationType.system,
        title="Added to a team",
        body=f"You were added to {employer.org_name} on MedHunt.",
        data={"employer_id": employer_id}))
    db.commit()
    if invitee.email:
        send_team_invite(invitee.email, employer.org_name)
    return {"added": True, "user_id": invitee.user_id, "email": invitee.email,
            "team_id": team.team_id}


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
        if actor_role == "manager":
            managed = team_access.managed_team_ids(db, employer, user)
            memberships = db.scalars(select(OrganizationTeamMember).where(
                OrganizationTeamMember.team_id.in_(managed),
                OrganizationTeamMember.user_id == member_user_id,
            )).all() if managed else []
            removable = [row for row in memberships if row.team_role == "member"]
            if not removable:
                raise HTTPException(403, "This member is not assigned to a team you manage.")
            for membership in removable:
                db.delete(membership)
            remaining = db.scalar(select(OrganizationTeamMember.team_member_id)
                                  .join(OrganizationTeam)
                                  .where(
                                      OrganizationTeam.employer_id == employer_id,
                                      OrganizationTeamMember.user_id == member_user_id,
                                      OrganizationTeamMember.team_id.not_in(
                                          [row.team_id for row in removable]
                                      ),
                                      OrganizationTeamMember.status == "active",
                                  ))
            if not remaining:
                permission = team_access.messaging_permission(db, employer_id, member_user_id)
                if permission:
                    permission.status = "paused"
                    permission.reason = "Removed from all active teams"
                    permission.updated_by_user_id = user.user_id
                    permission.permission_version += 1
            db.add(AuditLog(
                actor_user_id=user.user_id, action="organization_team_member_removed",
                entity_type="user", entity_id=member_user_id,
                meta={"employer_id": employer_id,
                      "team_ids": [row.team_id for row in removable]},
            ))
            db.commit()
            return
        team_ids = list(db.scalars(select(OrganizationTeam.team_id).where(
            OrganizationTeam.employer_id == employer_id,
        )).all())
        if team_ids:
            db.execute(delete(OrganizationTeamMember).where(
                OrganizationTeamMember.team_id.in_(team_ids),
                OrganizationTeamMember.user_id == member_user_id,
            ))
        db.execute(delete(MedhuntSmsSender).where(
            MedhuntSmsSender.employer_id == employer_id,
            MedhuntSmsSender.user_id == member_user_id,
        ))
        db.execute(delete(MedhuntMessagingPermission).where(
            MedhuntMessagingPermission.employer_id == employer_id,
            MedhuntMessagingPermission.user_id == member_user_id,
        ))
        db.add(AuditLog(
            actor_user_id=user.user_id, action="organization_member_removed",
            entity_type="user", entity_id=member_user_id,
            meta={"employer_id": employer_id, "previous_role": target_role},
        ))
        db.delete(m)
        db.commit()


class MemberRoleUpdate(BaseModel):
    member_role: str


class MemberQuickSourcerLimitUpdate(BaseModel):
    limit_override: Optional[int] = Field(default=None, ge=1, le=80)


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


@router.patch("/{employer_id}/members/{member_user_id}/quick-sourcer-limit")
def set_member_quick_sourcer_limit(
    employer_id: str,
    member_user_id: str,
    body: MemberQuickSourcerLimitUpdate,
    user: CurrentUser,
    db: DbSession,
):
    """Set or clear a member's extension lookup limit override; owner/admin only."""
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(status_code=404, detail="Employer not found")
    _require_cap(db, employer, user, "settings")
    membership = db.scalar(select(EmployerMember).where(
        EmployerMember.employer_id == employer_id,
        EmployerMember.user_id == member_user_id,
    ))
    if not membership:
        raise HTTPException(status_code=404, detail="Not a member of this organisation")
    membership.quick_sourcer_limit_override = body.limit_override
    db.add(AuditLog(
        actor_user_id=user.user_id,
        action="organization_member_quick_sourcer_limit_updated",
        entity_type="user",
        entity_id=member_user_id,
        meta={
            "employer_id": employer_id,
            "limit_override": body.limit_override,
            "organization_default": int(employer.quick_sourcer_per_user_limit or 10),
        },
    ))
    db.commit()
    return {
        "user_id": member_user_id,
        "limit_override": membership.quick_sourcer_limit_override,
        "effective_limit": int(
            membership.quick_sourcer_limit_override
            if membership.quick_sourcer_limit_override is not None
            else employer.quick_sourcer_per_user_limit or 10
        ),
    }


@router.get("/{employer_id}/usage")
def org_usage(employer_id: str, user: CurrentUser, db: DbSession):
    """Per-member usage for the org — credits and contacts revealed — so a
    manager/admin can 'track user usage' and see billing at a glance."""
    from ..models import AuditLog, CreditAccount
    from .profiles import RELEASE_ACTION
    from .extension import MEDHUNT_ENRICHED_ACTION

    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(status_code=404, detail="Employer not found")
    _require_cap(db, employer, user, "analytics")

    members = db.scalars(select(EmployerMember).where(
        EmployerMember.employer_id == employer_id)).all()
    scoped_ids = team_access.scoped_member_ids(db, employer, user)
    ids = sorted(scoped_ids)
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
    medhunt_enriched = dict(db.execute(
        select(AuditLog.actor_user_id, func.count())
        .where(AuditLog.actor_user_id.in_(ids),
               AuditLog.action == MEDHUNT_ENRICHED_ACTION)
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
            "medhunt_enriched": medhunt_enriched.get(uid, 0),
        })
    rows.sort(key=lambda x: (-org_roles.rank(x["role"]), -x["reveals"]))
    totals = {
        "credits": sum(r["credits"] for r in rows),
        "reveals": sum(r["reveals"] for r in rows),
        "medhunt_enriched": sum(r["medhunt_enriched"] for r in rows),
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
    team_id: Optional[str] = None


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
    team = _team_for_new_member(db, employer, user, body.team_id)
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
    inv = TeamInvite(employer_id=employer_id, team_id=team.team_id, email=email, role=role,
                     token_hash=sha256(raw), status="pending",
                     invited_by_user_id=user.user_id,
                     expires_at=utcnow() + timedelta(days=14))
    db.add(inv)
    db.commit()
    link = f"{settings.frontend_base_url.rstrip('/')}/?invite={raw}"
    send_team_invite(email, employer.org_name, accept_link=link)
    return {"invite_id": inv.invite_id, "email": email, "role": role,
            "team_id": team.team_id, "status": "pending"}


@router.get("/{employer_id}/invites")
def list_invites(employer_id: str, user: CurrentUser, db: DbSession):
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(status_code=404, detail="Employer not found")
    role = _require_cap(db, employer, user, "manage_members")
    filters = [TeamInvite.employer_id == employer_id, TeamInvite.status == "pending"]
    if role == "manager":
        filters.append(TeamInvite.team_id.in_(team_access.managed_team_ids(db, employer, user)))
    invs = db.scalars(select(TeamInvite).where(*filters)
        .order_by(TeamInvite.created_at.desc())).all()
    return {"items": [{"invite_id": i.invite_id, "email": i.email, "role": i.role,
                       "team_id": i.team_id,
                       "created_at": i.created_at, "expires_at": i.expires_at} for i in invs]}


@router.delete("/{employer_id}/invites/{invite_id}", status_code=204)
def revoke_invite(employer_id: str, invite_id: str, user: CurrentUser, db: DbSession):
    employer = db.get(Employer, employer_id)
    if not employer:
        return
    role = _require_cap(db, employer, user, "manage_members")
    inv = db.get(TeamInvite, invite_id)
    if inv and inv.employer_id == employer_id and inv.status == "pending":
        if role == "manager" and inv.team_id not in team_access.managed_team_ids(db, employer, user):
            raise HTTPException(403, "You can revoke invites only for teams you manage.")
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
    if user.email.strip().lower() != inv.email.strip().lower():
        raise HTTPException(
            status_code=403,
            detail=f"This invitation was sent to {inv.email}. Sign in with that email to accept it.",
        )

    already = user.user_id == employer.owner_user_id or bool(db.scalar(
        select(EmployerMember).where(EmployerMember.employer_id == inv.employer_id,
                                     EmployerMember.user_id == user.user_id)))
    if not already:
        db.add(EmployerMember(employer_id=inv.employer_id, user_id=user.user_id,
                              member_role=inv.role))
        team = db.get(OrganizationTeam, inv.team_id) if inv.team_id else None
        if not team or team.employer_id != inv.employer_id or team.status != "active":
            team = db.scalar(select(OrganizationTeam).where(
                OrganizationTeam.employer_id == inv.employer_id,
                OrganizationTeam.status == "active",
            ).order_by(func.lower(OrganizationTeam.name) != "general", OrganizationTeam.name))
        if not team:
            team = OrganizationTeam(
                employer_id=inv.employer_id,
                name="General",
                created_by_user_id=inv.invited_by_user_id,
            )
            db.add(team)
            db.flush()
        db.add(OrganizationTeamMember(
            team_id=team.team_id,
            user_id=user.user_id,
            team_role="manager" if inv.role == "manager" else "member",
            created_by_user_id=inv.invited_by_user_id,
        ))
    # Invitees need recruiter-level platform access for the organization pages
    # and sourcing tools, regardless of which public sign-up role was selected.
    if user.role == UserRole.job_seeker:
        user.role = UserRole.recruiter
    inv.status = "accepted"
    db.commit()
    return {
        "joined": True,
        "already": already,
        "employer": {
            "employer_id": employer.employer_id,
            "org_name": employer.org_name,
            "org_type": employer.org_type,
            "city": employer.city,
            "state_code": employer.state_code,
            "website_url": employer.website_url,
            "description": employer.description,
            "is_verified": employer.is_verified,
            "rating_avg": float(employer.rating_avg or 0),
        },
        # Retained for older clients.
        "org_name": employer.org_name,
    }
