"""Shared FastAPI dependencies: DB session and current-user resolution."""
from __future__ import annotations

from typing import Annotated, Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from .database import get_db
from .models import User, UserStatus
from .models.enums import UserRole
from .security import ACCESS_TOKEN, decode_token

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
