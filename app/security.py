"""Password hashing, JWT tokens and MFA (TOTP) helpers."""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any

import jwt
import pyotp
from passlib.context import CryptContext

from .config import settings
from .database import utcnow

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ACCESS_TOKEN = "access"
REFRESH_TOKEN = "refresh"


# --- Passwords ------------------------------------------------------------

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except ValueError:
        return False


# --- JWT ------------------------------------------------------------------

def _create_token(subject: str, token_type: str, expires: timedelta, **extra: Any) -> str:
    now = utcnow()
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires,
        **extra,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: str, role: str, session_id: str | None = None) -> str:
    extra = {"role": role, "jti": secrets.token_hex(8)}
    if session_id:
        extra["sid"] = session_id   # ties the token to one login for single-session
    return _create_token(
        user_id,
        ACCESS_TOKEN,
        timedelta(minutes=settings.access_token_expire_minutes),
        **extra,
    )


def create_web_session_token(user_id: str, role: str, session_id: str | None = None) -> str:
    """Longer-lived access token for server-rendered cookie sessions."""
    extra = {"role": role, "jti": secrets.token_hex(8)}
    if session_id:
        extra["sid"] = session_id
    return _create_token(
        user_id,
        ACCESS_TOKEN,
        timedelta(days=settings.refresh_token_expire_days),
        **extra,
    )


def create_refresh_token(user_id: str) -> str:
    # jti makes each refresh token unique even when issued in the same second,
    # so their hashes never collide on the sessions.refresh_token_hash unique key.
    return _create_token(
        user_id,
        REFRESH_TOKEN,
        timedelta(days=settings.refresh_token_expire_days),
        jti=secrets.token_hex(16),
    )


def decode_token(token: str) -> dict[str, Any]:
    """Raises jwt.PyJWTError subclasses on invalid/expired tokens."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


# --- Opaque token hashing (for storing refresh tokens / reset tokens) -----

def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_opaque_token(n_bytes: int = 32) -> str:
    return secrets.token_urlsafe(n_bytes)


# --- MFA (TOTP) -----------------------------------------------------------

def generate_mfa_secret() -> str:
    return pyotp.random_base32()


def mfa_provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name="HealthBoard")


def verify_mfa_code(secret: str, code: str) -> bool:
    return pyotp.TOTP(secret).verify(code, valid_window=1)
