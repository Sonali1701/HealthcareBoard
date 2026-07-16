"""Application lifecycle: candidate's own applications/saved jobs + ATS stage
transitions performed by recruiters."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ..database import utcnow
from ..deps import CurrentUser, DbSession
from ..models import (
    Application,
    ApplicationEvent,
    Employer,
    EmployerMember,
    JobPosting,
    Notification,
    Profile,
    SavedJob,
)
from ..models.enums import ApplicationStatus, NotificationType
from ..schemas.job import (
    ApplicationEventOut,
    ApplicationOut,
    ApplicationStageUpdate,
    JobOut,
)

router = APIRouter(prefix="/api/applications", tags=["applications"])


def _my_profile(db: DbSession, user: CurrentUser) -> Profile:
    profile = db.scalar(select(Profile).where(Profile.user_id == user.user_id))
    if not profile:
        raise HTTPException(status_code=400, detail="No profile for this account")
    return profile


@router.get("/mine", response_model=list[ApplicationOut])
def my_applications(user: CurrentUser, db: DbSession):
    profile = _my_profile(db, user)
    return db.scalars(
        select(Application)
        .where(Application.profile_id == profile.profile_id)
        .order_by(Application.applied_at.desc())
    ).all()


@router.get("/saved", response_model=list[JobOut])
def my_saved_jobs(user: CurrentUser, db: DbSession):
    profile = _my_profile(db, user)
    job_ids = db.scalars(
        select(SavedJob.job_id).where(SavedJob.profile_id == profile.profile_id)
    ).all()
    if not job_ids:
        return []
    return db.scalars(select(JobPosting).where(JobPosting.job_id.in_(job_ids))).all()


@router.get("/{application_id}", response_model=ApplicationOut)
def get_application(application_id: str, user: CurrentUser, db: DbSession):
    app = db.get(Application, application_id)
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    _assert_can_view(db, app, user)
    return app


@router.get("/{application_id}/events", response_model=list[ApplicationEventOut])
def application_events(application_id: str, user: CurrentUser, db: DbSession):
    app = db.get(Application, application_id)
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    _assert_can_view(db, app, user)
    return db.scalars(
        select(ApplicationEvent)
        .where(ApplicationEvent.application_id == application_id)
        .order_by(ApplicationEvent.created_at.asc())
    ).all()


@router.patch("/{application_id}/stage", response_model=ApplicationOut)
def update_stage(application_id: str, body: ApplicationStageUpdate,
                 user: CurrentUser, db: DbSession):
    app = db.get(Application, application_id)
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    job = db.get(JobPosting, app.job_id)
    _assert_recruiter(db, job, user)

    old = app.status
    app.status = body.status
    app.status_updated_at = utcnow()
    if body.recruiter_rating is not None:
        app.recruiter_rating = body.recruiter_rating
    if body.note:
        app.recruiter_notes = body.note

    db.add(ApplicationEvent(
        application_id=application_id,
        from_status=old.value,
        to_status=body.status.value,
        note=body.note,
        actor_user_id=user.user_id,
    ))

    # Notify the candidate.
    profile = db.get(Profile, app.profile_id)
    if profile and profile.user_id:
        db.add(Notification(
            user_id=profile.user_id,
            type=NotificationType.application,
            title="Application update",
            body=f"Your application for {job.title} is now '{body.status.value}'",
            data={"application_id": application_id, "status": body.status.value},
        ))
    db.commit()
    db.refresh(app)
    return app


# --- authorization helpers ------------------------------------------------

def _assert_recruiter(db: DbSession, job: JobPosting, user: CurrentUser) -> None:
    employer = db.get(Employer, job.employer_id)
    if employer.owner_user_id == user.user_id or user.role.value == "admin":
        return
    member = db.scalar(
        select(EmployerMember).where(
            EmployerMember.employer_id == employer.employer_id,
            EmployerMember.user_id == user.user_id,
        )
    )
    if not member:
        raise HTTPException(status_code=403, detail="Recruiter access required")


def _assert_can_view(db: DbSession, app: Application, user: CurrentUser) -> None:
    profile = db.get(Profile, app.profile_id)
    if profile and profile.user_id == user.user_id:
        return
    job = db.get(JobPosting, app.job_id)
    _assert_recruiter(db, job, user)
