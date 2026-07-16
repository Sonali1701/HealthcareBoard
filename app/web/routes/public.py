"""Public pages: landing, job search, job detail, talent browse + profile."""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ...database import get_db
from ...models import (
    Application,
    Employer,
    JobPosting,
    JobStatus,
    Profile,
    SavedJob,
)
from ..core import current_user, render

router = APIRouter(tags=["web-public"])
DbDep = Annotated[Session, Depends(get_db)]

SPECIALTIES = ["Allergy & Immunology", "ICU", "ER", "OR", "Labor & Delivery",
               "Med-Surg", "Telemetry", "Oncology", "Pediatrics", "Anesthesia"]
JOB_TYPES = ["travel", "staff", "per_diem", "contract"]


def _job_query(db, q=None, specialty=None, state=None, job_type=None, pay_min=None):
    stmt = select(JobPosting).where(JobPosting.status == JobStatus.active)
    if q:
        stmt = stmt.where(JobPosting.search_text.like(f"%{q.lower()}%"))
    if specialty:
        stmt = stmt.where(JobPosting.specialty == specialty)
    if state:
        stmt = stmt.where(JobPosting.state_code == state.upper())
    if job_type:
        stmt = stmt.where(JobPosting.job_type == job_type)
    if pay_min:
        stmt = stmt.where(JobPosting.pay_rate_max >= pay_min)
    return stmt


def _employer_map(db, jobs):
    ids = {j.employer_id for j in jobs}
    if not ids:
        return {}
    return {e.employer_id: e for e in db.scalars(select(Employer).where(Employer.employer_id.in_(ids)))}


@router.get("/")
def landing(request: Request, db: DbDep, user=Depends(current_user)):
    jobs = db.scalars(
        _job_query(db).order_by(JobPosting.is_featured.desc(), JobPosting.created_at.desc()).limit(6)
    ).all()
    stats = {
        "jobs": db.scalar(select(func.count()).select_from(JobPosting).where(JobPosting.status == JobStatus.active)) or 0,
        "candidates": db.scalar(select(func.count()).select_from(Profile)) or 0,
        "employers": db.scalar(select(func.count()).select_from(Employer)) or 0,
    }
    return render(request, "public/landing.html",
                  {"jobs": jobs, "employers": _employer_map(db, jobs), "stats": stats,
                   "specialties": SPECIALTIES, "user": user})


@router.get("/jobs")
def jobs_page(request: Request, db: DbDep, user=Depends(current_user),
              q: Optional[str] = None, specialty: Optional[str] = None,
              state: Optional[str] = None, job_type: Optional[str] = None,
              pay_min: Optional[float] = None):
    stmt = _job_query(db, q, specialty, state, job_type, pay_min)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    jobs = db.scalars(
        stmt.order_by(JobPosting.is_featured.desc(), JobPosting.created_at.desc()).limit(50)
    ).all()
    ctx = {"jobs": jobs, "employers": _employer_map(db, jobs), "total": total,
           "active": "jobs", "specialties": SPECIALTIES, "job_types": JOB_TYPES,
           "f": {"q": q or "", "specialty": specialty or "", "state": state or "",
                 "job_type": job_type or "", "pay_min": pay_min or ""}, "user": user}
    # HTMX: return just the results list for live filtering.
    if request.headers.get("HX-Request"):
        return render(request, "public/_job_results.html", ctx)
    return render(request, "public/jobs.html", ctx)


@router.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: str, db: DbDep, user=Depends(current_user)):
    job = db.get(JobPosting, job_id)
    if not job:
        return render(request, "public/not_found.html", {"what": "job"}, status_code=404)
    job.view_count = (job.view_count or 0) + 1
    db.commit()
    employer = db.get(Employer, job.employer_id)

    applied = saved = False
    if user:
        prof = db.scalar(select(Profile).where(Profile.user_id == user.user_id))
        if prof:
            applied = db.scalar(select(Application).where(
                Application.job_id == job_id, Application.profile_id == prof.profile_id)) is not None
            saved = db.scalar(select(SavedJob).where(
                SavedJob.job_id == job_id, SavedJob.profile_id == prof.profile_id)) is not None
    return render(request, "public/job_detail.html",
                  {"job": job, "employer": employer, "applied": applied, "saved": saved,
                   "active": "jobs", "user": user})


@router.get("/talent")
def talent_page(request: Request, db: DbDep, user=Depends(current_user),
                q: Optional[str] = None, specialty: Optional[str] = None,
                profession: Optional[str] = None, state: Optional[str] = None):
    stmt = select(Profile)
    if q:
        stmt = stmt.where(Profile.search_text.like(f"%{q.lower()}%"))
    if specialty:
        stmt = stmt.where(Profile.specialty == specialty)
    if profession:
        stmt = stmt.where(Profile.profession_type == profession)
    if state:
        stmt = stmt.where(Profile.state_code == state.upper())
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    profiles = db.scalars(stmt.order_by(Profile.completion_score.desc()).limit(60)).all()
    ctx = {"profiles": profiles, "total": total, "active": "talent",
           "specialties": SPECIALTIES,
           "f": {"q": q or "", "specialty": specialty or "", "profession": profession or "", "state": state or ""},
           "user": user}
    if request.headers.get("HX-Request"):
        return render(request, "public/_talent_results.html", ctx)
    return render(request, "public/talent.html", ctx)


@router.get("/talent/{profile_id}")
def talent_detail(request: Request, profile_id: str, db: DbDep, user=Depends(current_user)):
    profile = db.scalar(
        select(Profile).options(
            selectinload(Profile.licenses), selectinload(Profile.certifications),
            selectinload(Profile.work_history), selectinload(Profile.skills),
        ).where(Profile.profile_id == profile_id)
    )
    if not profile:
        return render(request, "public/not_found.html", {"what": "profile"}, status_code=404)
    return render(request, "public/talent_detail.html",
                  {"profile": profile, "active": "talent", "user": user})
