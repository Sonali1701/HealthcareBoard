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
    User,
)
from ..models.enums import ApplicationStatus, NotificationType
from ..services.email import send_application_update
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


@router.get("/mine/detail")
def my_applications_detail(user: CurrentUser, db: DbSession):
    """Applications with the role attached, and where each one stands.

    `/mine` returns raw rows carrying a job_id, so a professional could see
    that they had applied to *something* but not to what. Applying felt like a
    black hole; this is what makes it visible.
    """
    profile = _my_profile(db, user)
    apps = db.scalars(
        select(Application)
        .where(Application.profile_id == profile.profile_id)
        .order_by(Application.applied_at.desc())
    ).all()
    if not apps:
        return {"items": [], "by_status": {}}

    jobs = {j.job_id: j for j in db.scalars(
        select(JobPosting).where(JobPosting.job_id.in_([a.job_id for a in apps])))}
    orgs = {e.employer_id: e.org_name for e in db.scalars(
        select(Employer).where(Employer.employer_id.in_(
            [j.employer_id for j in jobs.values()])))} if jobs else {}

    # The order a candidate actually moves through, so the UI can show progress
    # rather than a bare status word.
    order = ["applied", "screening", "interview", "offer", "hired"]
    items = []
    for a in apps:
        job = jobs.get(a.job_id)
        status = a.status.value if hasattr(a.status, "value") else str(a.status)
        closed = status in {"rejected", "withdrawn"}
        items.append({
            "application_id": a.application_id,
            "job_id": a.job_id,
            "title": job.title if job else "Role no longer listed",
            "employer": orgs.get(job.employer_id) if job else None,
            "facility": getattr(job, "facility", None) if job else None,
            "location": ", ".join(x for x in [getattr(job, "city", None),
                                              getattr(job, "state_code", None)] if x) if job else None,
            "pay_rate_max": float(job.pay_rate_max) if job and job.pay_rate_max else None,
            "status": status,
            "stage_index": order.index(status) if status in order else None,
            "stages": order,
            "is_closed": closed,
            "applied_at": a.applied_at,
            "status_updated_at": a.status_updated_at,
        })
    by_status: dict[str, int] = {}
    for item in items:
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1
    return {"items": items, "by_status": by_status}


@router.post("/{application_id}/withdraw", response_model=ApplicationOut)
def withdraw_application(application_id: str, user: CurrentUser, db: DbSession):
    """Let a candidate pull out — the counterpart to applying."""
    profile = _my_profile(db, user)
    app_row = db.get(Application, application_id)
    if not app_row or app_row.profile_id != profile.profile_id:
        raise HTTPException(status_code=404, detail="Application not found")
    if app_row.status in {ApplicationStatus.hired, ApplicationStatus.withdrawn}:
        raise HTTPException(status_code=400,
                            detail=f"This application is already {app_row.status.value}")
    previous = app_row.status
    app_row.status = ApplicationStatus.withdrawn
    app_row.status_updated_at = utcnow()
    db.add(ApplicationEvent(
        application_id=app_row.application_id,
        from_status=previous.value if hasattr(previous, "value") else str(previous),
        to_status=ApplicationStatus.withdrawn.value,
        note="Withdrawn by the candidate"))
    db.commit()
    db.refresh(app_row)
    return app_row


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

    # Notify the candidate, in-app and by email.
    profile = db.get(Profile, app.profile_id)
    candidate_user = db.get(User, profile.user_id) if profile and profile.user_id else None
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
    if candidate_user and candidate_user.email:
        send_application_update(candidate_user.email, job.title, body.status.value)
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
