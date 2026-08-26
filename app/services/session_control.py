"""Single-active-session control (anti account-sharing).

One paid seat should be usable by one person at a time. When single-session is
enforced for a user's role (see settings.single_session_roles), every login
points ``users.active_session_id`` at the new session and revokes the account's
other sessions; any access token or web cookie whose ``sid`` claim doesn't match
the active session is then rejected on its next request — so a second login
signs the first device out.

Both auth paths funnel through here:
  * the JSON API (app/deps.get_current_user) checks ``session_ok``,
  * the Jinja web UI (app/web/core._user_from_request) checks it too,
  * every login (app/routers/auth._issue_tokens and the web login/signup) calls
    ``activate_session`` to claim the active slot and evict the rest.
"""
from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session as DbSession

from ..config import settings
from ..database import utcnow
from ..models import Session as UserSession, User


def enforced_for(role_value: str) -> bool:
    return settings.enforces_single_session(role_value)


def activate_session(db: DbSession, user: User, session_id: str) -> None:
    """Make ``session_id`` the account's active session.

    Always records it (so enabling enforcement later takes effect at once). When
    enforcement applies to this role, also revokes every other live session,
    which blocks an evicted device from rotating forward via /api/auth/refresh.
    Caller commits.
    """
    user.active_session_id = session_id
    if enforced_for(user.role.value):
        db.execute(
            update(UserSession)
            .where(
                UserSession.user_id == user.user_id,
                UserSession.session_id != session_id,
                UserSession.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        )


def session_ok(user: User, token_sid) -> bool:
    """False only when single-session is enforced for this user AND the token's
    session id doesn't match the account's current active session.

    A missing ``sid`` (legacy token issued before this feature, or before any
    post-migration login) fails the match once an active session exists — which
    is exactly the eviction we want. When no active session is recorded yet,
    everything is allowed, so deploying the feature never mass-signs-out anyone.
    """
    if not enforced_for(user.role.value):
        return True
    active = user.active_session_id
    if not active:
        return True
    return token_sid == active
