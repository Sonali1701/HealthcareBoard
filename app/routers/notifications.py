"""User notification feed."""
from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from ..database import utcnow
from ..deps import CurrentUser, DbSession
from ..models import Notification
from ..schemas.common import Message
from ..schemas.messaging import NotificationOut

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("", response_model=list[NotificationOut])
def list_notifications(user: CurrentUser, db: DbSession,
                       unread_only: bool = Query(False),
                       limit: int = Query(50, ge=1, le=200)):
    stmt = select(Notification).where(Notification.user_id == user.user_id)
    if unread_only:
        stmt = stmt.where(Notification.is_read.is_(False))
    return db.scalars(stmt.order_by(Notification.created_at.desc()).limit(limit)).all()


@router.get("/unread-count")
def unread_count(user: CurrentUser, db: DbSession):
    count = db.scalar(
        select(func.count()).select_from(Notification).where(
            Notification.user_id == user.user_id, Notification.is_read.is_(False)
        )
    )
    return {"unread": count or 0}


@router.post("/{notification_id}/read", response_model=Message)
def mark_read(notification_id: str, user: CurrentUser, db: DbSession):
    notif = db.get(Notification, notification_id)
    if notif and notif.user_id == user.user_id and not notif.is_read:
        notif.is_read = True
        notif.read_at = utcnow()
        db.commit()
    return Message(detail="ok")


@router.post("/read-all", response_model=Message)
def mark_all_read(user: CurrentUser, db: DbSession):
    for n in db.scalars(
        select(Notification).where(Notification.user_id == user.user_id,
                                   Notification.is_read.is_(False))
    ):
        n.is_read = True
        n.read_at = utcnow()
    db.commit()
    return Message(detail="ok")
