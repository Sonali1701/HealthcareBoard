"""Authentication & account endpoints: register, login, JWT refresh, MFA,
password reset, email verification, OAuth (stub)."""
from __future__ import annotations

from datetime import timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select

from ..config import settings
from ..database import new_uuid, utcnow
from ..deps import CurrentUser, DbSession
from ..ratelimit import auth_rate_limit
from ..services.session_control import activate_session
from ..models import (
    EmailVerificationToken,
    PasswordResetToken,
    Profile,
    Session,
    User,
    UserStatus,
)
from ..models.enums import ProfileSource
from ..schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    MFAEnrollResponse,
    MFAVerifyRequest,
    PasswordResetConfirm,
    PasswordResetRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserOut,
)
from ..schemas.common import Message
from ..services.email import send_email_verification, send_password_reset
from ..security import (
    REFRESH_TOKEN,
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_mfa_secret,
    generate_opaque_token,
    hash_password,
    mfa_provisioning_uri,
    sha256,
    verify_mfa_code,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def client_ip(request: Request | None) -> str | None:
    """The visitor's real IP. Behind Render's proxy, request.client.host is the
    load balancer, so prefer the left-most hop of X-Forwarded-For (the original
    client) when present."""
    if not request:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


def _issue_tokens(db: DbSession, user: User, request: Request | None = None) -> TokenPair:
    # Generate the session id up front so it can be stamped into the access token
    # as its `sid` claim — that claim is what single-session enforcement checks.
    session_id = new_uuid()
    access = create_access_token(user.user_id, user.role.value, session_id=session_id)
    refresh = create_refresh_token(user.user_id)
    session = Session(
        session_id=session_id,
        user_id=user.user_id,
        refresh_token_hash=sha256(refresh),
        device_info={"user_agent": request.headers.get("user-agent")} if request else {},
        ip_address=client_ip(request),
        expires_at=utcnow() + timedelta(days=settings.refresh_token_expire_days),
    )
    db.add(session)
    user.last_login_at = utcnow()
    # Claim the active-session slot and (when enforced) evict the other devices.
    activate_session(db, user, session_id)
    db.commit()
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_expire_minutes * 60,
    )


@router.post("/register", response_model=TokenPair, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, db: DbSession, request: Request,
             _rl: None = Depends(auth_rate_limit)):
    existing = db.scalar(select(User).where(User.email == body.email))
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    # When email is configured, require verification; otherwise auto-activate (dev).
    initial_status = (
        UserStatus.pending_verify if settings.email_enabled else UserStatus.active
    )
    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        role=body.role,
        status=initial_status,
    )
    db.add(user)
    db.flush()

    # Auto-create a profile for job seekers.
    if body.first_name or body.last_name:
        profile = Profile(
            user_id=user.user_id,
            first_name=body.first_name or "New",
            last_name=body.last_name or "User",
            source=ProfileSource.signup,
        )
        profile.rebuild_search_text()
        db.add(profile)

    # Issue an email-verification token and send it (if email is configured).
    raw_verify = generate_opaque_token()
    db.add(EmailVerificationToken(
        user_id=user.user_id,
        token_hash=sha256(raw_verify),
        expires_at=utcnow() + timedelta(days=2),
    ))
    db.commit()
    db.refresh(user)
    send_email_verification(user.email, raw_verify)
    return _issue_tokens(db, user, request)


def _login(body: LoginRequest, db: DbSession, request: Request) -> TokenPair:
    user = db.scalar(select(User).where(User.email == body.email))
    if not user or not user.password_hash or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if user.deleted_at is not None or user.status == UserStatus.suspended:
        raise HTTPException(status_code=403, detail="Account is not active")
    if user.mfa_enabled:
        if not body.mfa_code:
            raise HTTPException(status_code=401, detail="MFA code required")
        if not verify_mfa_code(user.mfa_secret or "", body.mfa_code):
            raise HTTPException(status_code=401, detail="Invalid MFA code")
    return _issue_tokens(db, user, request)


@router.post("/login", response_model=TokenPair)
def login(body: LoginRequest, db: DbSession, request: Request,
          _rl: None = Depends(auth_rate_limit)):
    return _login(body, db, request)


@router.post("/login/form", response_model=TokenPair, include_in_schema=False)
def login_form(db: DbSession, request: Request,
               form: OAuth2PasswordRequestForm = Depends()):
    """OAuth2 password-flow shim so the Swagger 'Authorize' button works."""
    return _login(LoginRequest(email=form.username, password=form.password), db, request)


@router.post("/refresh", response_model=TokenPair)
def refresh_token(body: RefreshRequest, db: DbSession, request: Request):
    try:
        payload = decode_token(body.refresh_token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    if payload.get("type") != REFRESH_TOKEN:
        raise HTTPException(status_code=401, detail="Not a refresh token")

    token_hash = sha256(body.refresh_token)
    session = db.scalar(select(Session).where(Session.refresh_token_hash == token_hash))
    if not session or session.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Session revoked or unknown")
    if session.expires_at < utcnow():
        raise HTTPException(status_code=401, detail="Session expired")

    user = db.get(User, payload["sub"])
    if not user or user.deleted_at is not None:
        raise HTTPException(status_code=401, detail="User not found")

    # Single active session: only the account's current active session may rotate
    # forward. This closes a race where two near-simultaneous logins could each
    # leave the other's refresh row un-revoked — the active-session pointer is the
    # single source of truth, so a superseded refresh token is rejected here too.
    if (settings.enforces_single_session(user.role.value)
            and user.active_session_id
            and session.session_id != user.active_session_id):
        raise HTTPException(
            status_code=401,
            detail="This account was signed in on another device.",
            headers={"X-Session-Superseded": "1"},
        )

    # Rotate: revoke the old session, issue a fresh pair.
    session.revoked_at = utcnow()
    db.commit()
    return _issue_tokens(db, user, request)


@router.post("/logout", response_model=Message)
def logout(body: RefreshRequest, db: DbSession):
    session = db.scalar(
        select(Session).where(Session.refresh_token_hash == sha256(body.refresh_token))
    )
    if session and session.revoked_at is None:
        session.revoked_at = utcnow()
        db.commit()
    return Message(detail="Logged out")


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser):
    return user


@router.post("/change-password", response_model=Message)
def change_password(body: ChangePasswordRequest, user: CurrentUser, db: DbSession):
    if not user.password_hash or not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    user.password_hash = hash_password(body.new_password)
    # Revoke all other sessions on password change.
    for s in db.scalars(select(Session).where(Session.user_id == user.user_id)):
        s.revoked_at = utcnow()
    db.commit()
    return Message(detail="Password changed")


# --- MFA ------------------------------------------------------------------

@router.post("/mfa/enroll", response_model=MFAEnrollResponse)
def mfa_enroll(user: CurrentUser, db: DbSession):
    secret = generate_mfa_secret()
    user.mfa_secret = secret  # stored unconfirmed until /mfa/verify
    db.commit()
    return MFAEnrollResponse(
        secret=secret,
        provisioning_uri=mfa_provisioning_uri(secret, user.email),
    )


@router.post("/mfa/verify", response_model=Message)
def mfa_verify(body: MFAVerifyRequest, user: CurrentUser, db: DbSession, request: Request,
               _rl: None = Depends(auth_rate_limit)):
    if not user.mfa_secret:
        raise HTTPException(status_code=400, detail="No MFA enrollment in progress")
    if not verify_mfa_code(user.mfa_secret, body.code):
        raise HTTPException(status_code=400, detail="Invalid MFA code")
    user.mfa_enabled = True
    db.commit()
    return Message(detail="MFA enabled")


@router.post("/mfa/disable", response_model=Message)
def mfa_disable(body: MFAVerifyRequest, user: CurrentUser, db: DbSession):
    if not user.mfa_enabled or not verify_mfa_code(user.mfa_secret or "", body.code):
        raise HTTPException(status_code=400, detail="Invalid MFA code")
    user.mfa_enabled = False
    user.mfa_secret = None
    db.commit()
    return Message(detail="MFA disabled")


# --- Password reset & email verification ----------------------------------
# The token is emailed. For local development (email disabled, non-production)
# it is ALSO returned as `dev_token` so a developer can complete the flow
# without a mail provider. It is never returned in production and never returned
# when email actually delivered it — see `_dev_token` below.

def _maybe_dev_token(resp: dict, raw: str, sent: bool) -> dict:
    """Expose a reset/verify token in the response only when it is safe to.

    Gated on the environment, NOT on `email_enabled`: production ships with
    email disabled on some hosts, and leaking the token there would let anyone
    reset any account. So: never in production, and never once it was emailed.
    """
    if not sent and settings.environment != "production":
        resp["dev_token"] = raw
    return resp


@router.post("/password-reset/request")
def password_reset_request(body: PasswordResetRequest, db: DbSession, request: Request,
                           _rl: None = Depends(auth_rate_limit)):
    user = db.scalar(select(User).where(User.email == body.email))
    # Always return success to avoid email enumeration.
    if not user:
        return {"detail": "If the email exists, a reset link was sent"}
    raw = generate_opaque_token()
    db.add(PasswordResetToken(
        user_id=user.user_id,
        token_hash=sha256(raw),
        expires_at=utcnow() + timedelta(hours=1),
    ))
    db.commit()
    sent = send_password_reset(user.email, raw)
    return _maybe_dev_token({"detail": "If the email exists, a reset link was sent"},
                            raw, sent)


@router.post("/password-reset/confirm", response_model=Message)
def password_reset_confirm(body: PasswordResetConfirm, db: DbSession):
    rec = db.scalar(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == sha256(body.token))
    )
    if not rec or rec.used_at is not None or rec.expires_at < utcnow():
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    user = db.get(User, rec.user_id)
    if not user:
        raise HTTPException(status_code=400, detail="Invalid token")
    user.password_hash = hash_password(body.new_password)
    rec.used_at = utcnow()
    db.commit()
    return Message(detail="Password reset successful")


@router.post("/email/request-verification")
def request_email_verification(user: CurrentUser, db: DbSession):
    raw = generate_opaque_token()
    db.add(EmailVerificationToken(
        user_id=user.user_id,
        token_hash=sha256(raw),
        expires_at=utcnow() + timedelta(days=2),
    ))
    db.commit()
    sent = send_email_verification(user.email, raw)
    return _maybe_dev_token({"detail": "Verification email sent"}, raw, sent)


@router.post("/email/verify", response_model=Message)
def verify_email(token: str, db: DbSession):
    rec = db.scalar(
        select(EmailVerificationToken).where(EmailVerificationToken.token_hash == sha256(token))
    )
    if not rec or rec.used_at is not None or rec.expires_at < utcnow():
        raise HTTPException(status_code=400, detail="Invalid or expired token")
    user = db.get(User, rec.user_id)
    user.email_verified_at = utcnow()
    if user.status == UserStatus.pending_verify:
        user.status = UserStatus.active
    rec.used_at = utcnow()
    db.commit()
    return Message(detail="Email verified")


@router.get("/oauth/{provider}", include_in_schema=False)
def oauth_start(provider: str):
    """OAuth entry point (stub). Wire real provider redirects here in prod."""
    raise HTTPException(
        status_code=501,
        detail=f"OAuth with {provider} is not configured in this environment",
    )
