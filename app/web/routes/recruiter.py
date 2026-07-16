"""Recruiter flows: dashboard, employer org, post/edit jobs, applicants + ATS."""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...database import get_db, utcnow
from ...models import (
    Application,
    ApplicationEvent,
    Employer,
    EmployerMember,
    JobPosting,
    JobStatus,
    JobType,
    Notification,
    Profile,
)
from ...models.enums import ApplicationStatus, NotificationType
from ..core import RedirectException, redirect, render, require_user

router = APIRouter(prefix="/recruiter", tags=["web-recruiter"])
DbDep = Annotated[Session, Depends(get_db)]


def _employer_for(db, user) -> Optional[Employer]:
    emp = db.scalar(select(Employer).where(Employer.owner_user_id == user.user_id))
    if emp:
        return emp
    member = db.scalar(select(EmployerMember).where(EmployerMember.user_id == user.user_id))
    return db.get(Employer, member.employer_id) if member else None


def _require_recruiter(user):
    if user.role.value not in ("recruiter", "employer", "admin"):
        raise RedirectException("/dashboard")


@router.get("")
def dashboard(request: Request, db: DbDep, user=Depends(require_user)):
    _require_recruiter(user)
    employer = _employer_for(db, user)
    if not employer:
        return render(request, "recruiter/no_employer.html", {"user": user})

    jobs = db.scalars(select(JobPosting).where(JobPosting.employer_id == employer.employer_id)
                      .order_by(JobPosting.created_at.desc())).all()
    job_ids = [j.job_id for j in jobs]
    apps = db.scalars(select(Application).where(Application.job_id.in_(job_ids))
                      .order_by(Application.applied_at.desc()).limit(8)).all() if job_ids else []
    prof_map = {p.profile_id: p for p in db.scalars(
        select(Profile).where(Profile.profile_id.in_([a.profile_id for a in apps])))} if apps else {}
    job_map = {j.job_id: j for j in jobs}
    kpis = {
        "jobs": len(jobs),
        "applicants": db.scalar(select(func.count()).select_from(Application).where(Application.job_id.in_(job_ids))) or 0 if job_ids else 0,
        "interviews": db.scalar(select(func.count()).select_from(Application).where(
            Application.job_id.in_(job_ids), Application.status == ApplicationStatus.interview)) or 0 if job_ids else 0,
        "hired": db.scalar(select(func.count()).select_from(Application).where(
            Application.job_id.in_(job_ids), Application.status == ApplicationStatus.hired)) or 0 if job_ids else 0,
    }
    return render(request, "recruiter/dashboard.html",
                  {"employer": employer, "jobs": jobs, "apps": apps,
                   "prof_map": prof_map, "job_map": job_map, "kpis": kpis, "user": user})


@router.get("/employer")
def employer_form(request: Request, db: DbDep, user=Depends(require_user)):
    _require_recruiter(user)
    return render(request, "recruiter/employer_edit.html",
                  {"employer": _employer_for(db, user), "user": user})


@router.post("/employer")
def employer_save(request: Request, db: DbDep, user=Depends(require_user),
                  org_name: Annotated[str, Form()] = "", org_type: Annotated[str, Form()] = "",
                  city: Annotated[str, Form()] = "", state_code: Annotated[str, Form()] = "",
                  website_url: Annotated[str, Form()] = "", description: Annotated[str, Form()] = ""):
    _require_recruiter(user)
    emp = _employer_for(db, user)
    if not emp:
        emp = Employer(owner_user_id=user.user_id, org_name=org_name.strip() or "My Organization")
        db.add(emp)
        db.flush()
        db.add(EmployerMember(employer_id=emp.employer_id, user_id=user.user_id, member_role="owner"))
    emp.org_name = org_name.strip() or emp.org_name
    emp.org_type = org_type.strip() or None
    emp.city = city.strip() or None
    emp.state_code = state_code.strip().upper() or None
    emp.website_url = website_url.strip() or None
    emp.description = description.strip() or None
    db.commit()
    return redirect("/recruiter", flash="Organization saved.")


@router.get("/jobs/new")
def job_new_form(request: Request, db: DbDep, user=Depends(require_user)):
    _require_recruiter(user)
    if not _employer_for(db, user):
        return redirect("/recruiter/employer", flash="Create your organization first.")
    return render(request, "recruiter/job_edit.html", {"job": None, "user": user})


@router.post("/jobs/new")
def job_create(request: Request, db: DbDep, user=Depends(require_user),
               title: Annotated[str, Form()] = "", specialty: Annotated[str, Form()] = "",
               profession_type: Annotated[str, Form()] = "", job_type: Annotated[str, Form()] = "travel",
               city: Annotated[str, Form()] = "", state_code: Annotated[str, Form()] = "",
               pay_rate_min: Annotated[str, Form()] = "", pay_rate_max: Annotated[str, Form()] = "",
               shift_type: Annotated[str, Form()] = "", description: Annotated[str, Form()] = "",
               required_skills: Annotated[str, Form()] = "", is_urgent: Annotated[str, Form()] = ""):
    _require_recruiter(user)
    employer = _employer_for(db, user)
    if not employer:
        return redirect("/recruiter/employer", flash="Create your organization first.")
    if not title.strip():
        return redirect("/recruiter/jobs/new", flash="Job title is required.", kind="error")
    job = JobPosting(
        employer_id=employer.employer_id, posted_by_user_id=user.user_id,
        title=title.strip(), specialty=specialty.strip() or None,
        profession_type=profession_type.strip() or None,
        job_type=JobType(job_type) if job_type in [t.value for t in JobType] else JobType.travel,
        shift_type=shift_type.strip() or None,
        pay_rate_min=float(pay_rate_min) if pay_rate_min.strip() else None,
        pay_rate_max=float(pay_rate_max) if pay_rate_max.strip() else None,
        city=city.strip() or None, state_code=state_code.strip().upper() or None,
        description=description.strip() or None,
        required_skills=[s.strip() for s in required_skills.split(",") if s.strip()],
        is_urgent=is_urgent == "on", status=JobStatus.active,
    )
    job.rebuild_search_text()
    db.add(job)
    db.commit()
    return redirect(f"/recruiter/jobs/{job.job_id}", flash="Job posted! 🎉")


@router.get("/jobs/{job_id}")
def job_manage(request: Request, job_id: str, db: DbDep, user=Depends(require_user)):
    _require_recruiter(user)
    job = db.get(JobPosting, job_id)
    employer = _employer_for(db, user)
    if not job or not employer or job.employer_id != employer.employer_id:
        return render(request, "public/not_found.html", {"what": "job"}, status_code=404)
    apps = db.scalars(select(Application).where(Application.job_id == job_id)
                      .order_by(Application.applied_at.desc())).all()
    prof_map = {p.profile_id: p for p in db.scalars(
        select(Profile).where(Profile.profile_id.in_([a.profile_id for a in apps])))} if apps else {}
    return render(request, "recruiter/job_manage.html",
                  {"job": job, "apps": apps, "prof_map": prof_map,
                   "stages": [s.value for s in ApplicationStatus], "user": user})


@router.post("/applications/{app_id}/stage")
def update_stage(request: Request, app_id: str, db: DbDep, user=Depends(require_user),
                 status: Annotated[str, Form()] = ""):
    _require_recruiter(user)
    app = db.get(Application, app_id)
    if not app:
        return redirect("/recruiter", flash="Application not found.", kind="error")
    job = db.get(JobPosting, app.job_id)
    employer = _employer_for(db, user)
    if not employer or job.employer_id != employer.employer_id:
        raise RedirectException("/recruiter")
    try:
        new_status = ApplicationStatus(status)
    except ValueError:
        return redirect(f"/recruiter/jobs/{app.job_id}", flash="Invalid status.", kind="error")
    old = app.status
    app.status = new_status
    app.status_updated_at = utcnow()
    db.add(ApplicationEvent(application_id=app_id, from_status=old.value,
                            to_status=new_status.value, actor_user_id=user.user_id))
    prof = db.get(Profile, app.profile_id)
    if prof and prof.user_id:
        db.add(Notification(user_id=prof.user_id, type=NotificationType.application,
                            title="Application update",
                            body=f"Your application for {job.title} is now '{new_status.value}'",
                            data={"job_id": app.job_id}))
    db.commit()
    return redirect(f"/recruiter/jobs/{app.job_id}", flash=f"Moved to {new_status.value}.")
