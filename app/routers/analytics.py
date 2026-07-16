"""Recruiter analytics: recruitment funnel + conversation/CRM table.

Backs the analytics view in healthboard-chat-platform.html.
"""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select

from ..deps import CurrentUser, DbSession
from ..models import (
    Application,
    Employer,
    EmployerMember,
    JobPosting,
    Message,
    MessageThread,
    Offer,
    Profile,
    User,
)
from ..models.enums import ApplicationStatus, OfferStatus

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def _employer_ids_for(db: DbSession, user: CurrentUser) -> list[str]:
    owned = db.scalars(
        select(Employer.employer_id).where(Employer.owner_user_id == user.user_id)
    ).all()
    member = db.scalars(
        select(EmployerMember.employer_id).where(EmployerMember.user_id == user.user_id)
    ).all()
    return list({*owned, *member})


@router.get("/funnel")
def recruitment_funnel(user: CurrentUser, db: DbSession):
    """Counts of applications per ATS stage across the recruiter's jobs."""
    employer_ids = _employer_ids_for(db, user)
    if not employer_ids:
        return {"stages": {}, "total": 0}

    job_ids = db.scalars(
        select(JobPosting.job_id).where(JobPosting.employer_id.in_(employer_ids))
    ).all()
    stages = {}
    total = 0
    if job_ids:
        rows = db.execute(
            select(Application.status, func.count())
            .where(Application.job_id.in_(job_ids))
            .group_by(Application.status)
        ).all()
        for status_val, count in rows:
            name = status_val.value if hasattr(status_val, "value") else str(status_val)
            stages[name] = count
            total += count
    # Ensure every stage is represented.
    funnel = {s.value: stages.get(s.value, 0) for s in ApplicationStatus}
    return {"stages": funnel, "total": total}


@router.get("/kpis")
def kpis(user: CurrentUser, db: DbSession):
    """Top-line CRM KPIs for the recruiter dashboard."""
    employer_ids = _employer_ids_for(db, user)
    job_ids = (
        db.scalars(select(JobPosting.job_id).where(JobPosting.employer_id.in_(employer_ids))).all()
        if employer_ids else []
    )

    active_conversations = db.scalar(
        select(func.count()).select_from(MessageThread).where(
            (MessageThread.participant_a_id == user.user_id)
            | (MessageThread.participant_b_id == user.user_id)
        )
    ) or 0

    def _count_status(status_val):
        if not job_ids:
            return 0
        return db.scalar(
            select(func.count()).select_from(Application).where(
                Application.job_id.in_(job_ids), Application.status == status_val
            )
        ) or 0

    offers_out = db.scalar(
        select(func.count()).select_from(Offer).where(
            Offer.recruiter_user_id == user.user_id, Offer.status == OfferStatus.sent
        )
    ) or 0

    return {
        "active_conversations": active_conversations,
        "in_interview": _count_status(ApplicationStatus.interview),
        "offers_out": offers_out,
        "hired": _count_status(ApplicationStatus.hired),
        "rejected": _count_status(ApplicationStatus.rejected),
    }


@router.get("/conversations")
def conversation_table(user: CurrentUser, db: DbSession):
    """Per-thread CRM rows: counterpart, ATS stage, message counts, last activity."""
    threads = db.scalars(
        select(MessageThread).where(
            (MessageThread.participant_a_id == user.user_id)
            | (MessageThread.participant_b_id == user.user_id)
        ).order_by(MessageThread.last_message_at.desc().nullslast())
    ).all()

    rows = []
    for t in threads:
        other_id = (t.participant_b_id if t.participant_a_id == user.user_id
                    else t.participant_a_id)
        counterpart = db.scalar(select(Profile).where(Profile.user_id == other_id))
        # Friendly name when the other party has no candidate profile (e.g. a
        # recruiter): use their organisation name, else their email handle.
        if counterpart:
            other_name = f"{counterpart.first_name} {counterpart.last_name}"
        else:
            emp = db.scalar(select(Employer).where(Employer.owner_user_id == other_id))
            other_user = db.get(User, other_id)
            other_name = (emp.org_name if emp else
                          (other_user.email.split("@")[0].replace(".", " ").title()
                           if other_user else other_id))
        sent = db.scalar(
            select(func.count()).select_from(Message).where(
                Message.thread_id == t.thread_id, Message.sender_id == user.user_id
            )
        ) or 0
        received = db.scalar(
            select(func.count()).select_from(Message).where(
                Message.thread_id == t.thread_id, Message.recipient_id == user.user_id
            )
        ) or 0
        response_rate = round(received / sent, 2) if sent else 0.0
        rows.append({
            "thread_id": t.thread_id,
            "candidate": other_name,
            "ats_stage": t.ats_stage,
            "messages_sent": sent,
            "messages_received": received,
            "response_rate": response_rate,
            "last_message_at": t.last_message_at,
        })
    return {"conversations": rows, "total": len(rows)}
