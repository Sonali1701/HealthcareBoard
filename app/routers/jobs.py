"""Job posting search/CRUD + applications + saved jobs."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import and_, func, select

from ..deps import CurrentUser, DbSession
from ..models import (
    Application,
    ApplicationEvent,
    Employer,
    EmployerMember,
    JobPosting,
    JobStatus,
    Notification,
    Profile,
    SavedJob,
)
from ..models.enums import ApplicationStatus, NotificationType
from ..schemas.common import Message, Page
from ..schemas.job import (
    ApplicationCreate,
    ApplicationEventOut,
    ApplicationOut,
    ApplicationStageUpdate,
    JobCreate,
    JobOut,
    JobUpdate,
    SavedJobOut,
)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _require_job_manager(db: DbSession, job: JobPosting, user: CurrentUser) -> Employer:
    employer = db.get(Employer, job.employer_id)
    if employer.owner_user_id == user.user_id or user.role.value == "admin":
        return employer
    member = db.scalar(
        select(EmployerMember).where(
            EmployerMember.employer_id == employer.employer_id,
            EmployerMember.user_id == user.user_id,
        )
    )
    if not member:
        raise HTTPException(status_code=403, detail="Cannot manage this job")
    return employer


def _current_profile(db: DbSession, user: CurrentUser) -> Profile:
    profile = db.scalar(select(Profile).where(Profile.user_id == user.user_id))
    if not profile:
        raise HTTPException(status_code=400, detail="Create a profile first")
    return profile


# --- Search & CRUD --------------------------------------------------------

@router.get("", response_model=Page[JobOut])
def search_jobs(
    db: DbSession,
    user: CurrentUser,
    q: Optional[str] = Query(None, description="Full-text search"),
    specialty: Optional[str] = None,
    profession_type: Optional[str] = None,
    job_type: Optional[str] = None,
    state_code: Optional[str] = None,
    city: Optional[str] = None,
    pay_min: Optional[float] = None,
    is_urgent: Optional[bool] = None,
    employer_id: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    stmt = select(JobPosting).where(JobPosting.status == JobStatus.active)
    if q:
        stmt = stmt.where(JobPosting.search_text.like(f"%{q.lower()}%"))
    if specialty:
        stmt = stmt.where(JobPosting.specialty == specialty)
    if profession_type:
        stmt = stmt.where(JobPosting.profession_type == profession_type)
    if job_type:
        stmt = stmt.where(JobPosting.job_type == job_type)
    if state_code:
        stmt = stmt.where(JobPosting.state_code == state_code.upper())
    if city:
        stmt = stmt.where(JobPosting.city.ilike(f"%{city}%"))
    if pay_min is not None:
        stmt = stmt.where(JobPosting.pay_rate_max >= pay_min)
    if is_urgent is not None:
        stmt = stmt.where(JobPosting.is_urgent.is_(is_urgent))
    if employer_id:
        stmt = stmt.where(JobPosting.employer_id == employer_id)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(JobPosting.is_featured.desc(), JobPosting.created_at.desc())
        .limit(limit).offset(offset)
    ).all()
    return Page(items=rows, total=total, limit=limit, offset=offset)


@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
def create_job(employer_id: str, body: JobCreate, user: CurrentUser, db: DbSession):
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(status_code=404, detail="Employer not found")
    if employer.owner_user_id != user.user_id and user.role.value != "admin":
        member = db.scalar(
            select(EmployerMember).where(
                EmployerMember.employer_id == employer_id,
                EmployerMember.user_id == user.user_id,
            )
        )
        if not member:
            raise HTTPException(status_code=403, detail="Cannot post for this employer")

    job = JobPosting(employer_id=employer_id, posted_by_user_id=user.user_id,
                     **body.model_dump())
    job.rebuild_search_text()
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: DbSession, user: CurrentUser):
    job = db.get(JobPosting, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.view_count += 1
    db.commit()
    db.refresh(job)
    return job


@router.patch("/{job_id}", response_model=JobOut)
def update_job(job_id: str, body: JobUpdate, user: CurrentUser, db: DbSession):
    job = db.get(JobPosting, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _require_job_manager(db, job, user)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(job, field, value)
    job.rebuild_search_text()
    db.commit()
    db.refresh(job)
    return job


@router.delete("/{job_id}", status_code=204)
def delete_job(job_id: str, user: CurrentUser, db: DbSession):
    job = db.get(JobPosting, job_id)
    if not job:
        return
    _require_job_manager(db, job, user)
    job.status = JobStatus.closed
    db.commit()


# --- Applications ---------------------------------------------------------

@router.post("/{job_id}/apply", response_model=ApplicationOut, status_code=201)
def apply_to_job(job_id: str, body: ApplicationCreate, user: CurrentUser, db: DbSession):
    job = db.get(JobPosting, job_id)
    if not job or job.status != JobStatus.active:
        raise HTTPException(status_code=404, detail="Job not available")
    profile = (
        db.get(Profile, body.profile_id) if body.profile_id
        else _current_profile(db, user)
    )
    if not profile:
        raise HTTPException(status_code=400, detail="Profile not found")

    existing = db.scalar(
        select(Application).where(
            and_(Application.job_id == job_id, Application.profile_id == profile.profile_id)
        )
    )
    if existing:
        raise HTTPException(status_code=409, detail="Already applied to this job")

    app = Application(
        job_id=job_id,
        profile_id=profile.profile_id,
        cover_letter=body.cover_letter,
        resume_snapshot_url=body.resume_snapshot_url or profile.resume_url,
        source=body.source,
    )
    db.add(app)
    job.application_count += 1
    db.flush()
    db.add(ApplicationEvent(application_id=app.application_id,
                            to_status=ApplicationStatus.applied.value,
                            actor_user_id=user.user_id))
    # Notify the employer owner.
    employer = db.get(Employer, job.employer_id)
    if employer:
        db.add(Notification(
            user_id=employer.owner_user_id,
            type=NotificationType.application,
            title="New application",
            body=f"{profile.first_name} {profile.last_name} applied to {job.title}",
            data={"job_id": job_id, "application_id": app.application_id},
        ))
    db.commit()
    db.refresh(app)
    return app


@router.get("/{job_id}/applications", response_model=list[ApplicationOut])
def list_job_applications(job_id: str, user: CurrentUser, db: DbSession,
                          status_filter: Optional[ApplicationStatus] = Query(None, alias="status")):
    job = db.get(JobPosting, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _require_job_manager(db, job, user)
    stmt = select(Application).where(Application.job_id == job_id)
    if status_filter:
        stmt = stmt.where(Application.status == status_filter)
    return db.scalars(stmt.order_by(Application.applied_at.desc())).all()


# --- Saved jobs -----------------------------------------------------------

@router.post("/{job_id}/save", response_model=SavedJobOut, status_code=201)
def save_job(job_id: str, user: CurrentUser, db: DbSession):
    job = db.get(JobPosting, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    profile = _current_profile(db, user)
    existing = db.scalar(
        select(SavedJob).where(
            and_(SavedJob.job_id == job_id, SavedJob.profile_id == profile.profile_id)
        )
    )
    if existing:
        return existing
    saved = SavedJob(job_id=job_id, profile_id=profile.profile_id)
    db.add(saved)
    db.commit()
    db.refresh(saved)
    return saved


@router.delete("/{job_id}/save", status_code=204)
def unsave_job(job_id: str, user: CurrentUser, db: DbSession):
    profile = _current_profile(db, user)
    saved = db.scalar(
        select(SavedJob).where(
            and_(SavedJob.job_id == job_id, SavedJob.profile_id == profile.profile_id)
        )
    )
    if saved:
        db.delete(saved)
        db.commit()
