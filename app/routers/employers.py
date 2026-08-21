"""Employer organisation endpoints."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select

from ..deps import CurrentUser, DbSession
from ..models import (
    Application,
    Employer,
    EmployerMember,
    JobPosting,
    Notification,
    Profile,
    User,
)
from ..models.enums import ApplicationStatus, NotificationType
from ..schemas.common import Page
from ..schemas.job import EmployerCreate, EmployerOut, EmployerUpdate
from ..services.email import send_team_invite

router = APIRouter(prefix="/api/employers", tags=["employers"])


class MemberInvite(BaseModel):
    email: EmailStr
    member_role: str = "member"


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
    _require_member(db, employer, user)
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

    can_manage = employer.owner_user_id == user.user_id or user.role.value == "admin"
    items = []
    for m in members:
        u = users.get(m.user_id)
        items.append({
            "user_id": m.user_id,
            "email": u.email if u else None,
            "name": _name(m.user_id),
            "member_role": m.member_role,
            "is_owner": m.user_id == employer.owner_user_id,
        })
    items.sort(key=lambda x: (not x["is_owner"], (x["name"] or x["email"] or "").lower()))
    return {"items": items, "can_manage": can_manage,
            "owner_user_id": employer.owner_user_id}


@router.post("/{employer_id}/members", status_code=201)
def invite_member(employer_id: str, body: MemberInvite, user: CurrentUser, db: DbSession):
    """Add an existing HealthBoard user to the organisation by email."""
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(status_code=404, detail="Employer not found")
    _require_owner(employer, user)

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
                          member_role=(body.member_role or "member")))
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
    _require_owner(employer, user)
    if member_user_id == employer.owner_user_id:
        raise HTTPException(status_code=400, detail="The owner cannot be removed")
    m = db.scalar(select(EmployerMember).where(
        EmployerMember.employer_id == employer_id,
        EmployerMember.user_id == member_user_id))
    if m:
        db.delete(m)
        db.commit()
