"""Messaging/CRM: threads, messages, ATS stages, interviews, offers."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select

from ..database import utcnow
from ..deps import CurrentUser, DbSession
from ..models import (
    Employer,
    EmployerMember,
    Interview,
    JobPosting,
    Message,
    MessageThread,
    Notification,
    Offer,
    Profile,
    User,
)
from ..models.enums import (
    InterviewStatus,
    MessageKind,
    NotificationType,
    OfferStatus,
)
from ..services.notifications import notify
from ..schemas.messaging import (
    ATSStageUpdate,
    EmailOutreachIn,
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
    ThreadSummary,
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


def _people(db: DbSession, user_ids: list[str]) -> dict[str, dict]:
    """Resolve display identities for a batch of user ids in two queries.

    Names live on the profile; users who never completed one fall back to the
    local part of their email so the inbox never shows a raw UUID.
    """
    ids = [u for u in {*user_ids} if u]
    if not ids:
        return {}
    names = {
        uid: " ".join(p for p in (first, last) if p).strip()
        for uid, first, last in db.execute(
            select(Profile.user_id, Profile.first_name, Profile.last_name)
            .where(Profile.user_id.in_(ids))
        ).all()
    }
    out: dict[str, dict] = {}
    for uid, email, role in db.execute(
        select(User.user_id, User.email, User.role).where(User.user_id.in_(ids))
    ).all():
        name = names.get(uid) or (email or "").split("@")[0] or "Unknown"
        parts = [p for p in name.replace(".", " ").split() if p]
        initials = "".join(p[0] for p in parts[:2]).upper() or "?"
        out[uid] = {"name": name, "role": getattr(role, "value", role),
                    "initials": initials}
    return out


def _unread_by_thread(db: DbSession, user: CurrentUser,
                      thread_ids: list[str]) -> dict[str, int]:
    if not thread_ids:
        return {}
    rows = db.execute(
        select(Message.thread_id, func.count())
        .where(Message.thread_id.in_(thread_ids),
               Message.recipient_id == user.user_id,
               Message.is_read.is_(False))
        .group_by(Message.thread_id)
    ).all()
    return {tid: n for tid, n in rows}


def _last_by_thread(db: DbSession, thread_ids: list[str]) -> dict[str, Message]:
    """Most recent message per thread (one query, newest-first de-dup)."""
    if not thread_ids:
        return {}
    out: dict[str, Message] = {}
    for m in db.scalars(
        select(Message).where(Message.thread_id.in_(thread_ids))
        .order_by(Message.created_at.desc())
    ):
        out.setdefault(m.thread_id, m)
    return out


def _summarise(thread: MessageThread, user: CurrentUser, people: dict,
               unread: dict, last: dict) -> ThreadSummary:
    other_id = _other(thread, user)
    who = people.get(other_id) or {}
    s = ThreadSummary.model_validate(thread)
    s.other_name = who.get("name", "Unknown")
    s.other_role = who.get("role")
    s.other_initials = who.get("initials", "?")
    s.unread_count = unread.get(thread.thread_id, 0)
    msg = last.get(thread.thread_id)
    if msg:
        s.last_message = msg.body or msg.kind.value.replace("_", " ").title()
        s.last_message_kind = msg.kind
        s.last_sender_is_me = msg.sender_id == user.user_id
    return s


# --- Threads --------------------------------------------------------------

@router.get("/threads", response_model=list[ThreadSummary])
def list_threads(user: CurrentUser, db: DbSession,
                 unread_only: bool = False):
    threads = db.scalars(
        select(MessageThread).where(
            or_(MessageThread.participant_a_id == user.user_id,
                MessageThread.participant_b_id == user.user_id)
        ).order_by(MessageThread.last_message_at.desc().nullslast())
    ).all()
    ids = [t.thread_id for t in threads]
    people = _people(db, [_other(t, user) for t in threads])
    unread = _unread_by_thread(db, user, ids)
    last = _last_by_thread(db, ids)
    out = [_summarise(t, user, people, unread, last) for t in threads]
    if unread_only:
        out = [t for t in out if t.unread_count]
    return out


@router.get("/unread-count")
def unread_count(user: CurrentUser, db: DbSession):
    """Badge counter for the nav — unread messages and how many threads they span."""
    total = db.scalar(
        select(func.count()).select_from(Message)
        .where(Message.recipient_id == user.user_id, Message.is_read.is_(False))
    ) or 0
    threads = db.scalar(
        select(func.count(func.distinct(Message.thread_id)))
        .where(Message.recipient_id == user.user_id, Message.is_read.is_(False))
    ) or 0
    return {"unread": total, "threads": threads}


@router.get("/can-message/{profile_id}")
def can_message(profile_id: str, user: CurrentUser, db: DbSession):
    """Whether this candidate is reachable in-app, and why not if they are not.

    Almost every profile came from an imported résumé and has no account, so
    the UI needs to know before it offers a Message button — otherwise it
    invites an action that always fails.
    """
    profile = db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    if not profile.user_id:
        return {"can_message": False,
                "reason": "This candidate has no account yet. Release their contact "
                          "details and reach them by email instead."}
    if profile.user_id == user.user_id:
        return {"can_message": False, "reason": "That is your own profile."}
    existing = db.scalar(
        select(MessageThread).where(
            or_(and_(MessageThread.participant_a_id == user.user_id,
                     MessageThread.participant_b_id == profile.user_id),
                and_(MessageThread.participant_a_id == profile.user_id,
                     MessageThread.participant_b_id == user.user_id))))
    return {"can_message": True,
            "thread_id": existing.thread_id if existing else None}


@router.post("/email-outreach")
def email_outreach(body: EmailOutreachIn, user: CurrentUser, db: DbSession):
    """Message an off-platform candidate by email (cold outreach).

    For candidates with no HealthBoard account. Gated behind the contact
    reveal — the recruiter must have released this candidate first — and the
    reply is routed to the recruiter's own inbox, not back into the app.
    """
    if user.role.value not in ("recruiter", "employer", "admin"):
        raise HTTPException(status_code=403, detail="Only recruiters can send outreach.")
    profile = db.get(Profile, body.profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    if profile.user_id:
        raise HTTPException(
            status_code=400,
            detail="This candidate is on HealthBoard — message them in-app instead.")
    if not profile.email:
        raise HTTPException(status_code=400, detail="No email on file for this candidate.")

    # Gate on the reveal: only email candidates whose contact you've released.
    from .profiles import _released_profile_ids
    if not _released_profile_ids(db, user, [profile.profile_id]):
        raise HTTPException(
            status_code=402,
            detail="Reveal this candidate's contact before emailing them.")

    # Recruiter identity for the message + reply-to.
    emp = db.scalar(select(Employer).where(Employer.owner_user_id == user.user_id))
    if not emp:
        member = db.scalar(select(EmployerMember).where(EmployerMember.user_id == user.user_id))
        emp = db.get(Employer, member.employer_id) if member else None
    from_label = emp.org_name if emp else "A recruiter on HealthBoard"

    from ..services.email import send_recruiter_message
    sent = send_recruiter_message(
        profile.email,
        candidate_name=(profile.first_name or "").strip(),
        from_label=from_label,
        reply_to=user.email,
        subject=body.subject,
        message=body.body,
    )
    if not sent:
        raise HTTPException(
            status_code=502, detail="The email could not be sent right now — please try again.")
    return {"sent": True, "email": profile.email}


@router.post("/threads", response_model=ThreadDetail, status_code=201)
def create_thread(body: ThreadCreate, user: CurrentUser, db: DbSession):
    recipient_id = body.recipient_id
    if not recipient_id and body.profile_id:
        profile = db.get(Profile, body.profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="Profile not found")
        if not profile.user_id:
            raise HTTPException(
                status_code=400,
                detail="This candidate has no platform account yet — release their "
                       "contact details and reach out by email or phone instead.")
        recipient_id = profile.user_id
    # Seeker-initiated: message the recruiter who posted a job (no recipient/
    # profile given, just the job). Resolves to the poster, else the org owner.
    if not recipient_id and body.job_id:
        job = db.get(JobPosting, body.job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        recipient_id = job.posted_by_user_id
        if not recipient_id:
            emp = db.get(Employer, job.employer_id)
            recipient_id = emp.owner_user_id if emp else None
        # Imported/ATS jobs are owned by a system account — no one to chat with.
        if recipient_id:
            owner = db.get(User, recipient_id)
            if owner and (owner.email or "").endswith("@system.local"):
                raise HTTPException(
                    status_code=400,
                    detail="This role was imported from an ATS and has no recruiter to "
                           "message here — apply to register your interest instead.")
    if not recipient_id:
        raise HTTPException(status_code=400,
                            detail="Provide a recipient, a profile, or a job")
    if recipient_id == user.user_id:
        raise HTTPException(status_code=400, detail="You cannot message yourself")
    if not db.get(User, recipient_id):
        raise HTTPException(status_code=404, detail="Recipient not found")
    # Reuse an existing thread between these two users if present.
    thread = db.scalar(
        select(MessageThread).where(
            or_(
                and_(MessageThread.participant_a_id == user.user_id,
                     MessageThread.participant_b_id == recipient_id),
                and_(MessageThread.participant_a_id == recipient_id,
                     MessageThread.participant_b_id == user.user_id),
            )
        )
    )
    if not thread:
        thread = MessageThread(
            participant_a_id=user.user_id,
            participant_b_id=recipient_id,
            job_id=body.job_id,
        )
        db.add(thread)
        db.flush()

    if body.body:
        _persist_message(db, thread, user, MessageCreate(body=body.body))
    db.commit()
    return _thread_detail(db, thread, user)


@router.get("/threads/{thread_id}", response_model=ThreadDetail)
def get_thread(thread_id: str, user: CurrentUser, db: DbSession,
               limit: int = Query(50, ge=1, le=200),
               before: str | None = Query(None, description="Load history older than this message id")):
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
    return _thread_detail(db, thread, user, limit=limit, before=before)


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
    # In-app bell + an email to the recipient's inbox.
    notify(db, user_id=recipient_id, type=NotificationType.message,
           title="You have a new message",
           body=(body.body or body.kind.value)[:140],
           data={"thread_id": thread.thread_id})
    return msg


def _thread_detail(db: DbSession, thread: MessageThread, user: CurrentUser,
                   limit: int = 50, before: str | None = None) -> ThreadDetail:
    """Newest `limit` messages, oldest-first for rendering.

    The whole thread used to be re-fetched on every open and on every poll
    tick. That is fine at five messages and wasteful at five hundred, so the
    read is paged: newest first in SQL, reversed for display, with `before`
    walking backwards through older history.
    """
    total = db.scalar(
        select(func.count()).select_from(Message)
        .where(Message.thread_id == thread.thread_id)) or 0

    window = select(Message).where(Message.thread_id == thread.thread_id)
    if before:
        anchor = db.get(Message, before)
        if anchor:
            window = window.where(Message.created_at < anchor.created_at)
    newest = db.scalars(
        window.order_by(Message.created_at.desc()).limit(limit + 1)).all()
    has_more = len(newest) > limit
    messages = list(reversed(newest[:limit]))
    other_id = _other(thread, user)
    who = _people(db, [other_id]).get(other_id) or {}
    detail = ThreadDetail.model_validate(thread)
    detail.messages = [MessageOut.model_validate(m) for m in messages]
    detail.total_messages = total
    detail.has_more = has_more
    detail.unread_count = db.scalar(
        select(func.count()).select_from(Message).where(
            Message.thread_id == thread.thread_id,
            Message.recipient_id == user.user_id,
            Message.is_read.is_(False))) or 0
    detail.other_name = who.get("name", "Unknown")
    detail.other_role = who.get("role")
    detail.other_initials = who.get("initials", "?")
    if newest:
        last = newest[0]            # newest-first ordering
        detail.last_message = last.body or last.kind.value.replace("_", " ").title()
        detail.last_message_kind = last.kind
        detail.last_sender_is_me = last.sender_id == user.user_id
    return detail


# --- Interviews -----------------------------------------------------------

@router.get("/interviews", response_model=list[InterviewOut])
def list_interviews(user: CurrentUser, db: DbSession):
    """Interviews this recruiter scheduled, or that were proposed to them."""
    own_profiles = db.scalars(
        select(Profile.profile_id).where(Profile.user_id == user.user_id)).all()
    return db.scalars(
        select(Interview).where(
            or_(Interview.recruiter_user_id == user.user_id,
                Interview.profile_id.in_(own_profiles) if own_profiles else False)
        ).order_by(Interview.created_at.desc())
    ).all()


@router.get("/offers", response_model=list[OfferOut])
def list_offers(user: CurrentUser, db: DbSession):
    """Offers this recruiter extended, or that were extended to them."""
    own_profiles = db.scalars(
        select(Profile.profile_id).where(Profile.user_id == user.user_id)).all()
    return db.scalars(
        select(Offer).where(
            or_(Offer.recruiter_user_id == user.user_id,
                Offer.profile_id.in_(own_profiles) if own_profiles else False)
        ).order_by(Offer.created_at.desc())
    ).all()


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
        notify(db, user_id=profile.user_id, type=NotificationType.system,
               title="Interview proposed",
               body="A recruiter proposed interview times",
               data={"interview_id": interview.interview_id})
    db.commit()
    db.refresh(interview)
    return interview


@router.post("/interviews/{interview_id}/confirm", response_model=InterviewOut)
def confirm_interview(interview_id: str, body: InterviewConfirm,
                      user: CurrentUser, db: DbSession):
    interview = db.get(Interview, interview_id)
    if not interview:
        raise HTTPException(status_code=404, detail="Interview not found")
    # Only the interview's recruiter or its candidate may confirm it — without
    # this any authenticated user could confirm any interview by guessing an id.
    profile = db.get(Profile, interview.profile_id)
    candidate_uid = profile.user_id if profile else None
    if user.user_id not in (interview.recruiter_user_id, candidate_uid) \
            and user.role.value != "admin":
        raise HTTPException(status_code=403, detail="Not a participant in this interview")
    interview.confirmed_slot = body.confirmed_slot
    interview.status = InterviewStatus.confirmed
    db.commit()
    db.refresh(interview)
    return interview


# --- Offers ---------------------------------------------------------------

@router.post("/offers", response_model=OfferOut, status_code=201)
def send_offer(body: OfferCreate, user: CurrentUser, db: DbSession):
    # Extending an offer is a recruiter action, and if it's attached to a thread
    # the sender must be a participant of that thread — otherwise anyone could
    # inject an offer into a conversation they're not part of.
    if user.role.value not in {"recruiter", "employer", "admin"}:
        raise HTTPException(status_code=403, detail="Only recruiters can extend offers")
    if body.thread_id:
        _thread_or_404(db, body.thread_id, user)
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
        notify(db, user_id=profile.user_id, type=NotificationType.system,
               title="You received an offer",
               body="A recruiter extended a formal offer",
               data={"offer_id": offer.offer_id})
    db.commit()
    db.refresh(offer)
    return offer


@router.post("/offers/{offer_id}/respond", response_model=OfferOut)
def respond_offer(offer_id: str, body: OfferRespond, user: CurrentUser, db: DbSession):
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(status_code=404, detail="Offer not found")
    # Only the candidate the offer was made to may accept or decline it — without
    # this any authenticated user could respond to anyone's offer by id.
    profile = db.get(Profile, offer.profile_id)
    candidate_uid = profile.user_id if profile else None
    if user.user_id != candidate_uid and user.role.value != "admin":
        raise HTTPException(status_code=403, detail="Only the candidate can respond to this offer")
    if body.status not in (OfferStatus.accepted, OfferStatus.declined):
        raise HTTPException(status_code=400, detail="Status must be accepted or declined")
    offer.status = body.status
    offer.responded_at = utcnow()
    notify(db, user_id=offer.recruiter_user_id, type=NotificationType.system,
           title=f"Offer {body.status.value}",
           body=f"The candidate {body.status.value} the offer",
           data={"offer_id": offer_id})
    db.commit()
    db.refresh(offer)
    return offer
