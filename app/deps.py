"""Shared FastAPI dependencies: DB session and current-user resolution."""
from __future__ import annotations

from typing import Annotated, Optional

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .models import User, UserStatus
from .models.enums import UserRole
from .security import ACCESS_TOKEN, decode_token
from .services.session_control import session_ok

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    db: DbSession,
    token: Annotated[Optional[str], Depends(oauth2_scheme)] = None,
) -> User:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_exc
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        raise credentials_exc
    if payload.get("type") != ACCESS_TOKEN:
        raise credentials_exc
    user_id = payload.get("sub")
    if not user_id:
        raise credentials_exc

    user = db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        raise credentials_exc
    if user.status == UserStatus.suspended:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account suspended")
    # Single active session: reject a token whose login was superseded by a newer
    # one on another device. The user is already loaded, so this costs no extra
    # query. The header lets the client show a specific "signed in elsewhere"
    # message rather than a generic auth error.
    if not session_ok(user, payload.get("sid")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This account was signed in on another device.",
            headers={"WWW-Authenticate": "Bearer", "X-Session-Superseded": "1"},
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_optional_user(
    db: DbSession,
    token: Annotated[Optional[str], Depends(oauth2_scheme)] = None,
) -> Optional[User]:
    if not token:
        return None
    try:
        return get_current_user(db=db, token=token)
    except HTTPException:
        return None


OptionalUser = Annotated[Optional[User], Depends(get_optional_user)]


def get_ingest_user(
    db: DbSession,
    token: Annotated[Optional[str], Depends(oauth2_scheme)] = None,
    x_capture_token: Annotated[Optional[str], Header()] = None,
) -> User:
    """Authenticate a capture request by a normal Bearer JWT OR the extension's
    long-lived X-Capture-Token header.

    Single-active-session (the sid check in get_current_user) deliberately does
    NOT apply to the X-Capture-Token path: it is a per-user API key for the
    browser extension, scoped to the two write-only /api/ingest/* endpoints
    (candidate/résumé capture) — it grants no read access to the paid directory,
    reveals, or messaging. Rotating it on every login would break the owner's own
    extension, so it stays exempt; treat it like an API key, not a login session.
    """
    if token:
        return get_current_user(db=db, token=token)
    if x_capture_token:
        user = db.scalar(select(User).where(User.capture_token == x_capture_token))
        if user and user.deleted_at is None and user.status != UserStatus.suspended:
            return user
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Provide a Bearer token or a valid X-Capture-Token",
        headers={"WWW-Authenticate": "Bearer"},
    )


IngestUser = Annotated[User, Depends(get_ingest_user)]


def require_roles(*roles: UserRole):
    """Dependency factory enforcing that the current user has one of ``roles``."""

    def checker(user: CurrentUser) -> User:
        if user.role not in roles and user.role != UserRole.admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions for this action",
            )
        return user

    return checker


# Platform super-admin: the owner of the job board. Gates the admin console.
AdminUser = Annotated[User, Depends(require_roles(UserRole.admin))]
