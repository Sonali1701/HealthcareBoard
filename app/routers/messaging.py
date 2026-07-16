"""Messaging/CRM: threads, messages, ATS stages, interviews, offers."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select

from ..database import utcnow
from ..deps import CurrentUser, DbSession
from ..models import (
    Interview,
    Message,
    MessageThread,
    Notification,
    Offer,
    Profile,
)
from ..models.enums import (
    InterviewStatus,
    MessageKind,
    NotificationType,
    OfferStatus,
)
from ..schemas.messaging import (
    ATSStageUpdate,
    InterviewConfirm,
    InterviewCreate,
    InterviewOut,
    MessageCreate,
    MessageOut,
    OfferCreate,
    OfferOut,
    OfferRespond,
    ThreadCreate,
    ThreadDetail,
    ThreadOut,
)

router = APIRouter(prefix="/api/messages", tags=["messaging"])


def _thread_or_404(db: DbSession, thread_id: str, user: CurrentUser) -> MessageThread:
    thread = db.get(MessageThread, thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    if user.user_id not in (thread.participant_a_id, thread.participant_b_id) \
            and user.role.value != "admin":
        raise HTTPException(status_code=403, detail="Not a participant")
    return thread


def _other(thread: MessageThread, user: CurrentUser) -> str:
    return thread.participant_b_id if thread.participant_a_id == user.user_id \
        else thread.participant_a_id


# --- Threads --------------------------------------------------------------

@router.get("/threads", response_model=list[ThreadOut])
def list_threads(user: CurrentUser, db: DbSession,
                 unread_only: bool = False):
    stmt = select(MessageThread).where(
        or_(MessageThread.participant_a_id == user.user_id,
            MessageThread.participant_b_id == user.user_id)
    ).order_by(MessageThread.last_message_at.desc().nullslast())
    threads = db.scalars(stmt).all()
    if unread_only:
        threads = [
            t for t in threads
            if db.scalar(
                select(func.count()).select_from(Message).where(
                    and_(Message.thread_id == t.thread_id,
                         Message.recipient_id == user.user_id,
                         Message.is_read.is_(False))
                )
            )
        ]
    return threads


@router.post("/threads", response_model=ThreadDetail, status_code=201)
def create_thread(body: ThreadCreate, user: CurrentUser, db: DbSession):
    # Reuse an existing thread between these two users if present.
    thread = db.scalar(
        select(MessageThread).where(
            or_(
                and_(MessageThread.participant_a_id == user.user_id,
                     MessageThread.participant_b_id == body.recipient_id),
                and_(MessageThread.participant_a_id == body.recipient_id,
                     MessageThread.participant_b_id == user.user_id),
            )
        )
    )
    if not thread:
        thread = MessageThread(
            participant_a_id=user.user_id,
            participant_b_id=body.recipient_id,
            job_id=body.job_id,
        )
        db.add(thread)
        db.flush()

    if body.body:
        _persist_message(db, thread, user, MessageCreate(body=body.body))
    db.commit()
    return _thread_detail(db, thread, user)


@router.get("/threads/{thread_id}", response_model=ThreadDetail)
def get_thread(thread_id: str, user: CurrentUser, db: DbSession):
    thread = _thread_or_404(db, thread_id, user)
    # Mark inbound messages read.
    for m in db.scalars(
        select(Message).where(and_(Message.thread_id == thread_id,
                                    Message.recipient_id == user.user_id,
                                    Message.is_read.is_(False)))
    ):
        m.is_read = True
        m.read_at = utcnow()
    db.commit()
    return _thread_detail(db, thread, user)


@router.post("/threads/{thread_id}/messages", response_model=MessageOut, status_code=201)
def send_message(thread_id: str, body: MessageCreate, user: CurrentUser, db: DbSession):
    thread = _thread_or_404(db, thread_id, user)
    msg = _persist_message(db, thread, user, body)
    db.commit()
    db.refresh(msg)
    return msg


@router.patch("/threads/{thread_id}/ats", response_model=ThreadOut)
def update_ats_stage(thread_id: str, body: ATSStageUpdate, user: CurrentUser, db: DbSession):
    thread = _thread_or_404(db, thread_id, user)
    thread.ats_stage = body.ats_stage
    db.commit()
    db.refresh(thread)
    return thread


def _persist_message(db: DbSession, thread: MessageThread, user: CurrentUser,
                     body: MessageCreate) -> Message:
    recipient_id = _other(thread, user)
    msg = Message(
        thread_id=thread.thread_id,
        sender_id=user.user_id,
        recipient_id=recipient_id,
        kind=body.kind,
        body=body.body,
        payload=body.payload,
    )
    db.add(msg)
    thread.last_message_at = utcnow()
    db.add(Notification(
        user_id=recipient_id,
        type=NotificationType.message,
        title="New message",
        body=(body.body or body.kind.value)[:140],
        data={"thread_id": thread.thread_id},
    ))
    return msg


def _thread_detail(db: DbSession, thread: MessageThread, user: CurrentUser) -> ThreadDetail:
    messages = db.scalars(
        select(Message).where(Message.thread_id == thread.thread_id)
        .order_by(Message.created_at.asc())
    ).all()
    unread = sum(1 for m in messages if m.recipient_id == user.user_id and not m.is_read)
    detail = ThreadDetail.model_validate(thread)
    detail.messages = [MessageOut.model_validate(m) for m in messages]
    detail.unread_count = unread
    return detail


# --- Interviews -----------------------------------------------------------

@router.post("/interviews", response_model=InterviewOut, status_code=201)
def schedule_interview(body: InterviewCreate, user: CurrentUser, db: DbSession):
    interview = Interview(
        recruiter_user_id=user.user_id,
        profile_id=body.profile_id,
        job_id=body.job_id,
        thread_id=body.thread_id,
        proposed_slots=[s.isoformat() for s in body.proposed_slots],
        location=body.location,
        notes=body.notes,
    )
    db.add(interview)
    # Optionally drop a schedule message into the thread.
    if body.thread_id:
        thread = db.get(MessageThread, body.thread_id)
        if thread:
            _persist_message(db, thread, user, MessageCreate(
                kind=MessageKind.schedule,
                body="Proposed interview times",
                payload={"slots": [s.isoformat() for s in body.proposed_slots],
                         "location": body.location},
            ))
    profile = db.get(Profile, body.profile_id)
    if profile and profile.user_id:
        db.add(Notification(
            user_id=profile.user_id,
            type=NotificationType.system,
            title="Interview proposed",
            body="A recruiter proposed interview times",
            data={"interview_id": interview.interview_id},
        ))
    db.commit()
    db.refresh(interview)
    return interview


@router.post("/interviews/{interview_id}/confirm", response_model=InterviewOut)
def confirm_interview(interview_id: str, body: InterviewConfirm,
                      user: CurrentUser, db: DbSession):
    interview = db.get(Interview, interview_id)
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")
    interview.confirmed_slot = body.confirmed_slot
    interview.status = InterviewStatus.confirmed
    db.commit()
    db.refresh(interview)
    return interview


# --- Offers ---------------------------------------------------------------

@router.post("/offers", response_model=OfferOut, status_code=201)
def send_offer(body: OfferCreate, user: CurrentUser, db: DbSession):
    offer = Offer(
        recruiter_user_id=user.user_id,
        job_id=body.job_id,
        profile_id=body.profile_id,
        thread_id=body.thread_id,
        pay_rate=body.pay_rate,
        pay_unit=body.pay_unit,
        start_date=body.start_date,
        details=body.details,
        expires_at=body.expires_at,
    )
    db.add(offer)
    if body.thread_id:
        thread = db.get(MessageThread, body.thread_id)
        if thread:
            _persist_message(db, thread, user, MessageCreate(
                kind=MessageKind.offer,
                body="Formal offer extended",
                payload={"pay_rate": body.pay_rate, "pay_unit": body.pay_unit,
                         "start_date": body.start_date.isoformat() if body.start_date else None},
            ))
    profile = db.get(Profile, body.profile_id)
    if profile and profile.user_id:
        db.add(Notification(
            user_id=profile.user_id,
            type=NotificationType.system,
            title="You received an offer",
            body="A recruiter extended a formal offer",
            data={"offer_id": offer.offer_id},
        ))
    db.commit()
    db.refresh(offer)
    return offer


@router.post("/offers/{offer_id}/respond", response_model=OfferOut)
def respond_offer(offer_id: str, body: OfferRespond, user: CurrentUser, db: DbSession):
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(status_code=404, detail="Offer not found")
    if body.status not in (OfferStatus.accepted, OfferStatus.declined):
        raise HTTPException(status_code=400, detail="Status must be accepted or declined")
    offer.status = body.status
    offer.responded_at = utcnow()
    db.add(Notification(
        user_id=offer.recruiter_user_id,
        type=NotificationType.system,
        title=f"Offer {body.status.value}",
        body=f"The candidate {body.status.value} the offer",
        data={"offer_id": offer_id},
    ))
    db.commit()
    db.refresh(offer)
    return offer
