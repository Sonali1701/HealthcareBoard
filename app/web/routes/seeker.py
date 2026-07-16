"""Job-seeker flows: dashboard, profile, résumé upload, apply, save, applications."""
from __future__ import annotations

import io
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import and_, select
from sqlalchemy.orm import Session, selectinload

from ...database import get_db
from ...importers.contact import backfill_missing_contact
from ...models import (
    Application,
    ApplicationEvent,
    Certification,
    Employer,
    JobPosting,
    JobStatus,
    License,
    Notification,
    Profile,
    ProfileSkill,
    SavedJob,
)
from ...models.enums import ApplicationStatus, LicenseStatus, NotificationType
from ...services import storage
from ..core import redirect, render, require_user

router = APIRouter(tags=["web-seeker"])
DbDep = Annotated[Session, Depends(get_db)]


def _profile(db, user) -> Optional[Profile]:
    return db.scalar(
        select(Profile).options(
            selectinload(Profile.licenses), selectinload(Profile.certifications),
            selectinload(Profile.skills), selectinload(Profile.work_history),
        ).where(Profile.user_id == user.user_id)
    )


def _ensure_profile(db, user) -> Profile:
    p = _profile(db, user)
    if not p:
        p = Profile(user_id=user.user_id, first_name="New", last_name="Member")
        p.rebuild_search_text()
        db.add(p)
        db.commit()
        db.refresh(p)
    return p


@router.get("/dashboard")
def dashboard(request: Request, db: DbDep, user=Depends(require_user)):
    profile = _ensure_profile(db, user)
    apps = db.scalars(
        select(Application).where(Application.profile_id == profile.profile_id)
        .order_by(Application.applied_at.desc())
    ).all()
    job_map = {j.job_id: j for j in db.scalars(
        select(JobPosting).where(JobPosting.job_id.in_([a.job_id for a in apps]))
    )} if apps else {}
    saved_ids = db.scalars(select(SavedJob.job_id).where(SavedJob.profile_id == profile.profile_id)).all()
    saved = db.scalars(select(JobPosting).where(JobPosting.job_id.in_(saved_ids))).all() if saved_ids else []
    # Recommended: same specialty, active.
    rec = []
    if profile.specialty:
        rec = db.scalars(
            select(JobPosting).where(JobPosting.status == JobStatus.active,
                                     JobPosting.specialty == profile.specialty).limit(4)
        ).all()
    return render(request, "seeker/dashboard.html",
                  {"profile": profile, "apps": apps, "job_map": job_map,
                   "saved": saved, "rec": rec, "user": user})


@router.get("/profile")
def profile_view(request: Request, db: DbDep, user=Depends(require_user)):
    profile = _ensure_profile(db, user)
    return render(request, "seeker/profile.html", {"profile": profile, "user": user})


@router.get("/profile/edit")
def profile_edit_form(request: Request, db: DbDep, user=Depends(require_user)):
    profile = _ensure_profile(db, user)
    return render(request, "seeker/profile_edit.html", {"profile": profile, "user": user})


@router.post("/profile/edit")
def profile_edit(
    request: Request, db: DbDep, user=Depends(require_user),
    first_name: Annotated[str, Form()] = "", last_name: Annotated[str, Form()] = "",
    headline: Annotated[str, Form()] = "", bio: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "", phone: Annotated[str, Form()] = "",
    specialty: Annotated[str, Form()] = "", profession_type: Annotated[str, Form()] = "",
    years_experience: Annotated[int, Form()] = 0, city: Annotated[str, Form()] = "",
    state_code: Annotated[str, Form()] = "", pay_min_hourly: Annotated[str, Form()] = "",
    open_to_work: Annotated[str, Form()] = "",
):
    p = _ensure_profile(db, user)
    p.first_name = first_name.strip() or p.first_name
    p.last_name = last_name.strip() or p.last_name
    p.headline = headline.strip() or None
    p.bio = bio.strip() or None
    p.email = email.strip() or None
    p.phone = phone.strip() or None
    p.specialty = specialty.strip() or None
    p.profession_type = profession_type.strip() or None
    p.years_experience = years_experience or 0
    p.city = city.strip() or None
    p.state_code = (state_code.strip().upper() or None)
    p.pay_min_hourly = float(pay_min_hourly) if pay_min_hourly.strip() else None
    p.open_to_work = open_to_work == "on"
    p.rebuild_search_text()
    _recompute_completion(p)
    db.commit()
    return redirect("/profile", flash="Profile updated.")


@router.post("/profile/resume")
def upload_resume(request: Request, db: DbDep, user=Depends(require_user),
                  file: UploadFile = File(...)):
    p = _ensure_profile(db, user)
    allowed = {"application/pdf",
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
               "application/msword"}
    if file.content_type not in allowed:
        return redirect("/profile/edit", flash="Résumé must be a PDF or Word doc.", kind="error")
    data = file.file.read()
    if len(data) > 10 * 1024 * 1024:
        return redirect("/profile/edit", flash="File exceeds 10 MB.", kind="error")
    key = storage.build_key(f"resumes/{p.profile_id}", file.filename or "resume")
    p.resume_url = storage.upload(io.BytesIO(data), key, file.content_type)
    backfill_missing_contact(p, data, file.filename or "resume")
    p.rebuild_search_text()
    _recompute_completion(p)
    db.commit()
    return redirect("/profile", flash="Résumé uploaded.")


@router.post("/profile/skill")
def add_skill(request: Request, db: DbDep, user=Depends(require_user),
              name: Annotated[str, Form()] = ""):
    p = _ensure_profile(db, user)
    if name.strip():
        db.add(ProfileSkill(profile_id=p.profile_id, name=name.strip()[:100]))
        p.rebuild_search_text()
        db.commit()
    return redirect("/profile/edit")


@router.post("/profile/license")
def add_license(request: Request, db: DbDep, user=Depends(require_user),
                license_type: Annotated[str, Form()] = "", state_code: Annotated[str, Form()] = "",
                license_number: Annotated[str, Form()] = ""):
    p = _ensure_profile(db, user)
    if license_type.strip() and state_code.strip():
        db.add(License(profile_id=p.profile_id, license_type=license_type.strip(),
                       state_code=state_code.strip().upper()[:2],
                       license_number=license_number.strip() or "(self-reported)",
                       status=LicenseStatus.active))
        db.commit()
    return redirect("/profile/edit")


# --- Apply / save (used from job detail) ----------------------------------

@router.post("/jobs/{job_id}/apply")
def apply(request: Request, job_id: str, db: DbDep, user=Depends(require_user)):
    job = db.get(JobPosting, job_id)
    if not job or job.status != JobStatus.active:
        return redirect("/jobs", flash="That job is no longer available.", kind="error")
    p = _ensure_profile(db, user)
    exists = db.scalar(select(Application).where(
        and_(Application.job_id == job_id, Application.profile_id == p.profile_id)))
    if exists:
        return redirect(f"/jobs/{job_id}", flash="You already applied to this job.")
    app = Application(job_id=job_id, profile_id=p.profile_id,
                      resume_snapshot_url=p.resume_url, source="platform")
    db.add(app)
    job.application_count = (job.application_count or 0) + 1
    db.flush()
    db.add(ApplicationEvent(application_id=app.application_id,
                            to_status=ApplicationStatus.applied.value, actor_user_id=user.user_id))
    employer = db.get(Employer, job.employer_id)
    if employer:
        db.add(Notification(user_id=employer.owner_user_id, type=NotificationType.application,
                            title="New application",
                            body=f"{p.first_name} {p.last_name} applied to {job.title}",
                            data={"job_id": job_id}))
    db.commit()
    return redirect(f"/jobs/{job_id}", flash="Application submitted! 🎉")


@router.post("/jobs/{job_id}/save")
def save(request: Request, job_id: str, db: DbDep, user=Depends(require_user)):
    p = _ensure_profile(db, user)
    existing = db.scalar(select(SavedJob).where(
        and_(SavedJob.job_id == job_id, SavedJob.profile_id == p.profile_id)))
    if existing:
        db.delete(existing); db.commit()
        return redirect(f"/jobs/{job_id}", flash="Removed from saved jobs.")
    db.add(SavedJob(job_id=job_id, profile_id=p.profile_id)); db.commit()
    return redirect(f"/jobs/{job_id}", flash="Saved.")


def _recompute_completion(p: Profile) -> None:
    score = 0
    score += 10 if p.headline else 0
    score += 10 if p.bio else 0
    score += 15 if p.specialty else 0
    score += 10 if p.profession_type else 0
    score += 10 if p.years_experience else 0
    score += 10 if (p.city and p.state_code) else 0
    score += 5 if (p.email or p.phone) else 0
    score += 5 if p.pay_min_hourly else 0
    score += 15 if p.resume_url else 0
    score += 10 if p.licenses else 0
    score += 5 if p.skills else 0
    p.completion_score = min(score, 100)
