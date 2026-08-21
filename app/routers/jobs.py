"""Job posting search/CRUD + applications + saved jobs."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import Integer, and_, func, select

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
    User,
)
from ..models.enums import ApplicationStatus, NotificationType
from ..services.email import send_new_application
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
    facility: Optional[str] = None,
    group_openings: bool = Query(
        False, description="Collapse identical roles at the same facility into "
                           "one row carrying an `openings` count"),
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
    if facility:
        stmt = stmt.where(JobPosting.facility == facility)

    if group_openings:
        return _grouped_page(db, stmt, limit, offset)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(JobPosting.is_featured.desc(), JobPosting.created_at.desc())
        .limit(limit).offset(offset)
    ).all()
    return Page(items=rows, total=total, limit=limit, offset=offset)


# Agencies file one requisition per seat, so a single role at one facility can
# appear 30 times with 30 distinct req codes. They are NOT duplicates and must
# not be merged in the database — but the board is unreadable without folding
# them into a single row that says how many seats are open.
_GROUP_KEY = (JobPosting.title, JobPosting.facility, JobPosting.city,
              JobPosting.state_code, JobPosting.pay_rate_max)


def _grouped_page(db, stmt, limit: int, offset: int) -> Page:
    base = stmt.subquery()
    groups = (
        select(*[getattr(base.c, c.key) for c in _GROUP_KEY],
               func.count().label("openings"),
               func.min(base.c.job_id).label("job_id"))
        .group_by(*[getattr(base.c, c.key) for c in _GROUP_KEY])
    ).subquery()

    total = db.scalar(select(func.count()).select_from(groups)) or 0
    rows = db.execute(
        select(groups.c.job_id, groups.c.openings)
        .order_by(groups.c.openings.desc(), groups.c.job_id)
        .limit(limit).offset(offset)
    ).all()
    openings = {jid: n for jid, n in rows}
    jobs = db.scalars(
        select(JobPosting).where(JobPosting.job_id.in_(list(openings)))
    ).all() if openings else []
    items = []
    for job in sorted(jobs, key=lambda j: -openings[j.job_id]):
        out = JobOut.model_validate(job)
        out.openings = openings[job.job_id]
        items.append(out)
    return Page(items=items, total=total, limit=limit, offset=offset)


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


@router.get("/recommended", response_model=Page[JobOut])
def recommended_jobs(user: CurrentUser, db: DbSession,
                     limit: int = Query(20, ge=1, le=50)):
    """Roles that fit the signed-in professional's own profile.

    The platform already scores candidates against a job for recruiters; this
    runs the same comparison from the other side. It is deliberately ordered
    rather than filtered — a nurse with an unusual specialty should still see
    the closest roles instead of an empty page.
    """
    from ..models import Profile

    profile = db.scalar(select(Profile).where(Profile.user_id == user.user_id))
    stmt = select(JobPosting).where(JobPosting.status == JobStatus.active)

    if not profile or not (profile.profession_type or profile.specialty
                           or profile.state_code):
        # Nothing to match on yet — show the newest roles rather than nothing,
        # and let the profile-completion prompt do the rest.
        rows = db.scalars(stmt.order_by(JobPosting.created_at.desc()).limit(limit)).all()
        return Page(items=rows, total=len(rows), limit=limit, offset=0)

    # Rank by how much of the profile a role matches, strongest signal first:
    # the licence has to line up for the job to be doable at all, specialty
    # decides whether they are competitive, and location decides whether they
    # would take it.
    score = (
        func.coalesce(
            (JobPosting.profession_type == profile.profession_type).cast(Integer) * 4, 0)
        + func.coalesce(
            (JobPosting.specialty == profile.specialty).cast(Integer) * 3, 0)
        + func.coalesce(
            (JobPosting.state_code == profile.state_code).cast(Integer) * 2, 0)
        + func.coalesce(
            (func.lower(JobPosting.city) == func.lower(profile.city)).cast(Integer), 0)
    ).label("fit")

    rows = db.execute(
        select(JobPosting, score)
        .where(JobPosting.status == JobStatus.active)
        .order_by(score.desc(), JobPosting.is_featured.desc(),
                  JobPosting.created_at.desc())
        .limit(limit)
    ).all()
    items = []
    for job, fit in rows:
        out = JobOut.model_validate(job)
        out.fit_score = int(fit or 0)
        items.append(out)
    return Page(items=items, total=len(items), limit=limit, offset=0)


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: DbSession, user: CurrentUser):
    job = db.get(JobPosting, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.view_count += 1
    db.commit()
    db.refresh(job)
    return job


@router.get("/{job_id}/pay-estimate")
async def job_pay_estimate(job_id: str, db: DbSession, user: CurrentUser):
    """Estimated weekly take-home for this job, GSA per-diem-aware.

    Treats the job's advertised hourly as a blended package (the clinician's own
    pay, not a bill rate) and splits it into taxable pay + tax-free per-diem for
    the job's location — the same model as the seeker Pay Tools tab. California
    daily overtime applies automatically.
    """
    from ..schemas.gsa import PayPackageRequest
    from ..services.gsa import calculate_pay_package, get_gsa_rates

    job = db.get(JobPosting, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    reqs = job.requirements or {}
    hours = float(reqs.get("hours_per_week") or 36) or 36
    hourly = float(job.pay_rate_max or job.pay_rate_min or 0)
    if not hourly and reqs.get("weekly_pay"):
        try:
            hourly = float(reqs["weekly_pay"]) / hours
        except (TypeError, ValueError, ZeroDivisionError):
            hourly = 0.0
    if not hourly or not job.state_code:
        return {"available": False}
    weeks = int(reqs.get("duration_weeks") or 13) or 13
    try:
        payreq = PayPackageRequest(
            bill_rate=hourly, city=(job.city or job.state_code), state_code=job.state_code,
            margin_pct=0, burden_multiplier=1.0, hours_per_week=hours,
            contract_weeks=min(max(weeks, 1), 104), tax_rate=0.22)
    except Exception:  # noqa: BLE001 — a bad rate/location just means "no estimate"
        return {"available": False}
    rates = await get_gsa_rates(payreq.city, payreq.state_code)
    r = calculate_pay_package(payreq, rates)
    pd = r.option_perdiem
    return {
        "available": True,
        "hourly": round(hourly, 2), "hours_per_week": hours,
        "weekly_net": pd.est_weekly_net, "weekly_tax_free": pd.weekly_tax_free,
        "weekly_taxable_gross": pd.weekly_taxable_gross, "weekly_total": pd.weekly_total,
        "overtime": r.breakdown["overtime"], "gsa_live": rates.source == "api.gsa.gov",
        "city": job.city, "state_code": job.state_code,
    }


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
    # Notify the employer owner, in-app and by email.
    employer = db.get(Employer, job.employer_id)
    candidate_name = f"{profile.first_name} {profile.last_name}".strip()
    if employer:
        db.add(Notification(
            user_id=employer.owner_user_id,
            type=NotificationType.application,
            title="New application",
            body=f"{candidate_name} applied to {job.title}",
            data={"job_id": job_id, "application_id": app.application_id},
        ))
    db.commit()
    db.refresh(app)
    if employer:
        owner = db.get(User, employer.owner_user_id)
        if owner and owner.email:
            send_new_application(owner.email, candidate_name, job.title)
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


# The candidate ATS pipeline, in the order applicants actually move through it.
# "rejected"/"withdrawn" are terminal and sit outside the ordered steps.
_APPLICANT_STAGES = ["applied", "screening", "interview", "offer", "hired"]


@router.get("/{job_id}/applicants")
def list_job_applicants(job_id: str, user: CurrentUser, db: DbSession,
                        status_filter: Optional[ApplicationStatus] = Query(None, alias="status")):
    """Applicants for a job, with each candidate's details attached.

    `/applications` returns bare rows carrying a profile_id, which is unusable
    as a review screen. Applying to a job reveals the candidate to that
    employer (the apply notification already names them), so this returns their
    real name and contact to the job's recruiters — the counterpart to the
    credit-gated reveal that governs the cold directory.
    """
    job = db.get(JobPosting, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _require_job_manager(db, job, user)

    stmt = select(Application).where(Application.job_id == job_id)
    if status_filter:
        stmt = stmt.where(Application.status == status_filter)
    apps = db.scalars(stmt.order_by(Application.applied_at.desc())).all()

    profs = {p.profile_id: p for p in db.scalars(
        select(Profile).where(Profile.profile_id.in_([a.profile_id for a in apps])))} if apps else {}

    items, by_status = [], {}
    for a in apps:
        p = profs.get(a.profile_id)
        status = a.status.value if hasattr(a.status, "value") else str(a.status)
        by_status[status] = by_status.get(status, 0) + 1
        items.append({
            "application_id": a.application_id,
            "profile_id": a.profile_id,
            "user_id": p.user_id if p else None,
            "name": f"{p.first_name} {p.last_name}".strip() if p else "Candidate",
            "headline": p.headline if p else None,
            "profession_type": p.profession_type if p else None,
            "specialty": p.specialty if p else None,
            "years_experience": p.years_experience if p else None,
            "location": ", ".join(x for x in [getattr(p, "city", None),
                                              getattr(p, "state_code", None)] if x) if p else None,
            "completion": p.completion_score if p else 0,
            "email": p.email if p else None,
            "phone": p.phone if p else None,
            "resume_url": p.resume_url if p else None,
            "cover_letter": a.cover_letter,
            "recruiter_rating": a.recruiter_rating,
            "recruiter_notes": a.recruiter_notes,
            "status": status,
            "stage_index": _APPLICANT_STAGES.index(status) if status in _APPLICANT_STAGES else None,
            "stages": _APPLICANT_STAGES,
            "is_closed": status in {"rejected", "withdrawn"},
            "applied_at": a.applied_at,
            "status_updated_at": a.status_updated_at,
        })
    return {
        "job": {"job_id": job.job_id, "title": job.title,
                "specialty": job.specialty, "profession_type": job.profession_type,
                "location": ", ".join(x for x in [job.city, job.state_code] if x)},
        "items": items,
        "by_status": by_status,
        "stages": _APPLICANT_STAGES,
    }


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
