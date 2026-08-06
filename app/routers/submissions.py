"""Submitting candidates to client facilities.

A pool records who the agency is considering. A submission records who they
actually put forward — the billable event, and the number a recruiter is
measured on. Statuses follow the client's process rather than the internal
shortlist, so they are deliberately not the pool stages.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..database import utcnow
from ..deps import CurrentUser, DbSession
from ..models import (
    SUBMISSION_STATUSES,
    Employer,
    EmployerMember,
    JobPosting,
    Profile,
    Submission,
    User,
)
from .profiles import _masked_name, _released_profile_ids, _require_provider_directory_access

router = APIRouter(prefix="/api/submissions", tags=["submissions"])


class SubmissionIn(BaseModel):
    profile_id: str
    job_id: Optional[str] = None
    pool_id: Optional[str] = None
    facility: Optional[str] = Field(default=None, max_length=200)
    bill_rate: Optional[float] = None
    pay_rate: Optional[float] = None
    note: Optional[str] = None


class SubmissionUpdate(BaseModel):
    status: Optional[str] = None
    bill_rate: Optional[float] = None
    pay_rate: Optional[float] = None
    note: Optional[str] = None


def _team_user_ids(db: DbSession, user: CurrentUser) -> list[str]:
    """This recruiter plus anyone else at the same agency.

    Submissions are an agency record, not a personal one — a colleague covering
    a desk has to be able to see what has already gone to the client.
    """
    employer_ids = list({
        *db.scalars(select(Employer.employer_id)
                    .where(Employer.owner_user_id == user.user_id)).all(),
        *db.scalars(select(EmployerMember.employer_id)
                    .where(EmployerMember.user_id == user.user_id)).all(),
    })
    if not employer_ids:
        return [user.user_id]
    mates = {
        *db.scalars(select(Employer.owner_user_id)
                    .where(Employer.employer_id.in_(employer_ids))).all(),
        *db.scalars(select(EmployerMember.user_id)
                    .where(EmployerMember.employer_id.in_(employer_ids))).all(),
        user.user_id,
    }
    return [m for m in mates if m]


def _check_status(value: str) -> str:
    if value not in SUBMISSION_STATUSES:
        raise HTTPException(status_code=400,
                            detail=f"status must be one of {', '.join(SUBMISSION_STATUSES)}")
    return value


@router.get("")
def list_submissions(user: CurrentUser, db: DbSession,
                     status: Optional[str] = Query(None),
                     limit: int = Query(100, ge=1, le=300)):
    _require_provider_directory_access(user)
    team = _team_user_ids(db, user)
    stmt = select(Submission).where(Submission.submitted_by_user_id.in_(team))
    if status:
        stmt = stmt.where(Submission.status == _check_status(status))
    rows = db.scalars(stmt.order_by(Submission.submitted_at.desc()).limit(limit)).all()

    profiles = {p.profile_id: p for p in db.scalars(
        select(Profile).where(Profile.profile_id.in_([r.profile_id for r in rows])))} if rows else {}
    jobs = {j.job_id: j for j in db.scalars(
        select(JobPosting).where(JobPosting.job_id.in_(
            [r.job_id for r in rows if r.job_id])))} if rows else {}
    who = {u.user_id: u.email for u in db.scalars(
        select(User).where(User.user_id.in_(team)))}
    released = _released_profile_ids(db, user, [r.profile_id for r in rows])

    counts = dict(db.execute(
        select(Submission.status, func.count())
        .where(Submission.submitted_by_user_id.in_(team))
        .group_by(Submission.status)).all())

    items = []
    for r in rows:
        p = profiles.get(r.profile_id)
        job = jobs.get(r.job_id) if r.job_id else None
        # Identity still follows the release rule — submitting someone does not
        # reveal them to a colleague who has not paid to see them.
        is_open = r.profile_id in released
        items.append({
            "submission_id": r.submission_id,
            "profile_id": r.profile_id,
            "candidate": (f"{p.first_name or ''} {p.last_name or ''}".strip()
                          if (p and is_open) else (_masked_name(p) if p else "Unknown")),
            "is_released": is_open,
            "profession_type": p.profession_type if p else None,
            "specialty": p.specialty if p else None,
            "job_id": r.job_id,
            "job_title": job.title if job else None,
            "facility": r.facility or (getattr(job, "facility", None) if job else None),
            "status": r.status,
            "bill_rate": float(r.bill_rate) if r.bill_rate is not None else None,
            "pay_rate": float(r.pay_rate) if r.pay_rate is not None else None,
            "margin": (float(r.bill_rate) - float(r.pay_rate))
                      if (r.bill_rate is not None and r.pay_rate is not None) else None,
            "note": r.note,
            "submitted_by": who.get(r.submitted_by_user_id),
            "submitted_at": r.submitted_at,
        })
    return {"items": items, "by_status": counts, "statuses": list(SUBMISSION_STATUSES),
            "team_size": len(team)}


@router.post("", status_code=201)
def submit_candidate(body: SubmissionIn, user: CurrentUser, db: DbSession):
    _require_provider_directory_access(user)
    profile = db.get(Profile, body.profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Candidate not found")

    facility, employer_id = body.facility, None
    if body.job_id:
        job = db.get(JobPosting, body.job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        facility = facility or getattr(job, "facility", None)
        employer_id = job.employer_id

    existing = db.scalar(select(Submission).where(
        Submission.profile_id == body.profile_id,
        Submission.job_id == body.job_id)) if body.job_id else None
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Already submitted to this role on "
                   f"{existing.submitted_at:%d %b %Y} ({existing.status})")

    sub = Submission(
        profile_id=body.profile_id, job_id=body.job_id, pool_id=body.pool_id,
        employer_id=employer_id, facility=facility,
        submitted_by_user_id=user.user_id,
        bill_rate=body.bill_rate, pay_rate=body.pay_rate, note=body.note)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return {"submission_id": sub.submission_id, "status": sub.status,
            "facility": sub.facility}


@router.patch("/{submission_id}")
def update_submission(submission_id: str, body: SubmissionUpdate,
                      user: CurrentUser, db: DbSession):
    _require_provider_directory_access(user)
    sub = db.get(Submission, submission_id)
    if not sub or sub.submitted_by_user_id not in _team_user_ids(db, user):
        raise HTTPException(status_code=404, detail="Submission not found")
    if body.status is not None:
        sub.status = _check_status(body.status)
        sub.status_updated_at = utcnow()
    for field in ("bill_rate", "pay_rate", "note"):
        value = getattr(body, field)
        if value is not None:
            setattr(sub, field, value)
    db.commit()
    return {"submission_id": sub.submission_id, "status": sub.status}


@router.delete("/{submission_id}", status_code=204)
def delete_submission(submission_id: str, user: CurrentUser, db: DbSession):
    _require_provider_directory_access(user)
    sub = db.get(Submission, submission_id)
    if sub and sub.submitted_by_user_id in _team_user_ids(db, user):
        db.delete(sub)
        db.commit()
