"""In-app messaging between recruiters and candidates."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ...database import get_db, utcnow
from ...models import Message, MessageThread, Notification, Profile, User
from ...models.enums import MessageKind, NotificationType
from ..core import redirect, render, require_user

router = APIRouter(prefix="/messages", tags=["web-messages"])
DbDep = Annotated[Session, Depends(get_db)]


def _thread_rows(db, user):
    threads = db.scalars(
        select(MessageThread).where(
            or_(MessageThread.participant_a_id == user.user_id,
                MessageThread.participant_b_id == user.user_id))
        .order_by(MessageThread.last_message_at.desc().nullslast())
    ).all()
    rows = []
    for t in threads:
        other_id = t.participant_b_id if t.participant_a_id == user.user_id else t.participant_a_id
        prof = db.scalar(select(Profile).where(Profile.user_id == other_id))
        other = db.get(User, other_id)
        name = f"{prof.first_name} {prof.last_name}" if prof else (other.email if other else "Unknown")
        rows.append({"thread": t, "name": name, "other_id": other_id})
    return rows


@router.get("")
def inbox(request: Request, db: DbDep, user=Depends(require_user)):
    rows = _thread_rows(db, user)
    return render(request, "messages/inbox.html",
                  {"rows": rows, "active_thread": None, "messages": [], "user": user})


@router.get("/{thread_id}")
def thread_view(request: Request, thread_id: str, db: DbDep, user=Depends(require_user)):
    thread = db.get(MessageThread, thread_id)
    if not thread or user.user_id not in (thread.participant_a_id, thread.participant_b_id):
        return redirect("/messages", flash="Conversation not found.", kind="error")
    # Mark inbound read.
    for m in db.scalars(select(Message).where(and_(
            Message.thread_id == thread_id, Message.recipient_id == user.user_id,
            Message.is_read.is_(False)))):
        m.is_read = True
        m.read_at = utcnow()
    db.commit()
    msgs = db.scalars(select(Message).where(Message.thread_id == thread_id)
                      .order_by(Message.created_at.asc())).all()
    rows = _thread_rows(db, user)
    cur = next((r for r in rows if r["thread"].thread_id == thread_id), None)
    return render(request, "messages/inbox.html",
                  {"rows": rows, "active_thread": thread, "messages": msgs,
                   "counterpart": cur["name"] if cur else "", "user": user})


@router.post("/{thread_id}/send")
def send(request: Request, thread_id: str, db: DbDep, user=Depends(require_user),
         body: Annotated[str, Form()] = ""):
    thread = db.get(MessageThread, thread_id)
    if not thread or user.user_id not in (thread.participant_a_id, thread.participant_b_id):
        return redirect("/messages", flash="Conversation not found.", kind="error")
    if body.strip():
        recipient = (thread.participant_b_id if thread.participant_a_id == user.user_id
                     else thread.participant_a_id)
        db.add(Message(thread_id=thread_id, sender_id=user.user_id, recipient_id=recipient,
                       kind=MessageKind.text, body=body.strip()))
        thread.last_message_at = utcnow()
        db.add(Notification(user_id=recipient, type=NotificationType.message,
                            title="New message", body=body.strip()[:120],
                            data={"thread_id": thread_id}))
        db.commit()
    return redirect(f"/messages/{thread_id}")


@router.get("/start/{profile_id}")
def start(request: Request, profile_id: str, db: DbDep, user=Depends(require_user)):
    profile = db.get(Profile, profile_id)
    if not profile:
        return redirect("/talent", flash="Candidate not found.", kind="error")
    if not profile.user_id:
        return redirect(f"/talent/{profile_id}",
                        flash="This candidate hasn't joined HealthBoard yet, so they can't be messaged.",
                        kind="error")
    if profile.user_id == user.user_id:
        return redirect("/messages")
    existing = db.scalar(select(MessageThread).where(or_(
        and_(MessageThread.participant_a_id == user.user_id, MessageThread.participant_b_id == profile.user_id),
        and_(MessageThread.participant_a_id == profile.user_id, MessageThread.participant_b_id == user.user_id))))
    if existing:
        return redirect(f"/messages/{existing.thread_id}")
    thread = MessageThread(participant_a_id=user.user_id, participant_b_id=profile.user_id)
    db.add(thread)
    db.commit()
    return redirect(f"/messages/{thread.thread_id}")
