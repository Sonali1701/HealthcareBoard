"""Create an in-app notification and (optionally) email it to the recipient.

One place so every person-to-person event — a new message, an offer, an
interview — lands both in the bell menu and in the recipient's inbox, without
each call site re-implementing the email lookup and send.
"""
from __future__ import annotations

from ..config import settings
from ..models import Notification, User
from ..models.enums import NotificationType

# Page each notification type deep-links to in the SPA.
_LINK_PAGE = {
    NotificationType.message: "messages",
    NotificationType.application: "applications",
}


def notify(db, *, user_id: str, type: NotificationType, title: str,
           body: str = "", data: dict | None = None, email: bool = True) -> Notification:
    n = Notification(user_id=user_id, type=type, title=title,
                     body=body or "", data=data or {})
    db.add(n)
    if email:
        user = db.get(User, user_id)
        if user and user.email:
            from .email import send_notification_email
            page = _LINK_PAGE.get(type)
            link = (settings.frontend_base_url.rstrip("/") + f"/?page={page}") if page else None
            # Never let an email failure break the request that triggered it.
            try:
                send_notification_email(user.email, title=title,
                                        body=body or title, cta_link=link)
            except Exception:  # noqa: BLE001
                pass
    return n
