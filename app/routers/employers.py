"""Employer organisation endpoints."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from ..deps import CurrentUser, DbSession
from ..models import Application, Employer, EmployerMember, JobPosting, Profile
from ..models.enums import ApplicationStatus
from ..schemas.common import Page
from ..schemas.job import EmployerCreate, EmployerOut, EmployerUpdate

router = APIRouter(prefix="/api/employers", tags=["employers"])


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
        "employer": {"org_name": emp.org_name, "org_type": emp.org_type, "city": emp.city,
                     "state_code": emp.state_code, "is_verified": emp.is_verified,
                     "rating_avg": float(emp.rating_avg or 0)},
        "kpis": {"jobs": len(jobs),
                 "applications": (db.scalar(select(func.count()).select_from(Application)
                                  .where(Application.job_id.in_(job_ids))) or 0) if job_ids else 0,
                 "interviews": _count(ApplicationStatus.interview),
                 "offers": _count(ApplicationStatus.offer),
                 "hired": _count(ApplicationStatus.hired)},
        "jobs": [{"job_id": j.job_id, "title": j.title, "job_type": j.job_type.value,
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
