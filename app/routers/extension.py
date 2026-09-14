"""Distribute + connect the browser capture extension.

The recruiter downloads the extension from here and connects it with a personal
capture token; the extension then POSTs candidates it captures on Indeed (and,
soon, other platforms) to /api/ingest/* as that recruiter.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import secrets
import zipfile
from datetime import timedelta
from pathlib import Path

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from ..config import settings
from ..database import utcnow
from ..deps import CurrentUser, DbSession, IngestUser
from ..models import AuditLog, EmailVerificationToken, User, UserRole, UserStatus
from ..ratelimit import auth_rate_limit
from ..security import sha256
from ..services.email import send_medhunt_login_code

router = APIRouter(prefix="/api/extension", tags=["extension"])

# The packaged Chrome/Edge extension (MV3) lives beside the job board.
_EXT_DIR = (Path(__file__).resolve().parent.parent.parent
            / "Radixsol_Sourcing_Assistant" / "src_pkg" / "frontend")
_VERSION = "2.5.2"
MEDHUNT_LOGIN_PURPOSE = "medhunt_email_code"
MEDHUNT_CODE_TTL_MINUTES = 10
MEDHUNT_ENRICHED_ACTION = "medhunt_candidate_enriched"
MEDHUNT_ATTEMPT_ACTION = "medhunt_enrichment_attempt"
_PLATFORMS = [
    {"name": "Indeed", "status": "live"},
    {"name": "Vivian", "status": "coming"},
    {"name": "ZipRecruiter", "status": "coming"},
    {"name": "Facebook", "status": "coming"},
]


def _require_recruiter(user: CurrentUser) -> None:
    if user.role.value not in {"recruiter", "admin"}:
        raise HTTPException(status_code=403,
                            detail="The capture extension is available to recruiter accounts.")


def _ensure_token(db, user: User) -> str:
    if not user.capture_token:
        user.capture_token = secrets.token_urlsafe(32)
        db.commit()
    return user.capture_token


class MedhuntCodeRequest(BaseModel):
    email: EmailStr


class MedhuntCodeVerify(BaseModel):
    email: EmailStr
    code: str = Field(pattern=r"^\d{6}$")
    challenge: str = Field(min_length=40, max_length=4096)


class MedhuntEnrichmentEvent(BaseModel):
    event_id: str = Field(min_length=8, max_length=36, pattern=r"^[A-Za-z0-9_-]+$")
    candidate_id: str = Field(min_length=1, max_length=80)
    status: str = Field(min_length=1, max_length=40)
    source: str = Field(default="", max_length=80)
    run_id: str = Field(default="", max_length=120)


def _code_digest(email: str, code: str, nonce: str) -> str:
    message = f"{MEDHUNT_LOGIN_PURPOSE}\0{email}\0{code}\0{nonce}".encode()
    return hmac.new(settings.jwt_secret.encode(), message, hashlib.sha256).hexdigest()


def _login_challenge(email: str, user_id: str, code: str) -> tuple[str, str]:
    nonce = secrets.token_urlsafe(18)
    challenge_id = secrets.token_hex(16)
    now = utcnow()
    payload = {
        "sub": user_id,
        "email": email,
        "purpose": MEDHUNT_LOGIN_PURPOSE,
        "nonce": nonce,
        "jti": challenge_id,
        "code_hash": _code_digest(email, code, nonce),
        "iat": now,
        "exp": now + timedelta(minutes=MEDHUNT_CODE_TTL_MINUTES),
    }
    return (
        jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm),
        challenge_id,
    )


def _medhunt_user(db, email: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email))
    if (
        not user
        or user.deleted_at is not None
        or user.status not in {UserStatus.active, UserStatus.pending_verify}
    ):
        return None
    if user.role not in {UserRole.recruiter, UserRole.admin}:
        return None
    return user


@router.post("/auth/request-code")
def request_medhunt_code(body: MedhuntCodeRequest, request: Request, db: DbSession,
                          _rl: None = Depends(auth_rate_limit)):
    """Email a short-lived code without revealing whether an account exists."""
    email = str(body.email).strip().lower()
    user = _medhunt_user(db, email)
    code = f"{secrets.randbelow(1_000_000):06d}"
    user_id = user.user_id if user else secrets.token_hex(18)
    challenge, challenge_id = _login_challenge(email, user_id, code)
    if user:
        db.add(EmailVerificationToken(
            user_id=user.user_id,
            token_hash=sha256(f"{MEDHUNT_LOGIN_PURPOSE}:{challenge_id}"),
            expires_at=utcnow() + timedelta(minutes=MEDHUNT_CODE_TTL_MINUTES),
        ))
        db.commit()
        send_medhunt_login_code(email, code, MEDHUNT_CODE_TTL_MINUTES)
    return {
        "detail": "If this email can use Medhunt, a sign-in code was sent.",
        "challenge": challenge,
        "expires_in": MEDHUNT_CODE_TTL_MINUTES * 60,
    }


@router.post("/auth/verify-code")
def verify_medhunt_code(body: MedhuntCodeVerify, request: Request, db: DbSession,
                         _rl: None = Depends(auth_rate_limit)):
    email = str(body.email).strip().lower()
    try:
        payload = jwt.decode(
            body.challenge, settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired sign-in code") from exc
    if payload.get("purpose") != MEDHUNT_LOGIN_PURPOSE or payload.get("email") != email:
        raise HTTPException(status_code=401, detail="Invalid or expired sign-in code")
    expected = _code_digest(email, body.code, str(payload.get("nonce") or ""))
    if not hmac.compare_digest(expected, str(payload.get("code_hash") or "")):
        raise HTTPException(status_code=401, detail="Invalid or expired sign-in code")
    user = _medhunt_user(db, email)
    if not user or payload.get("sub") != user.user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired sign-in code")
    challenge_id = str(payload.get("jti") or "")
    record = db.scalar(select(EmailVerificationToken).where(
        EmailVerificationToken.user_id == user.user_id,
        EmailVerificationToken.token_hash == sha256(
            f"{MEDHUNT_LOGIN_PURPOSE}:{challenge_id}"
        ),
    ))
    if not record or record.used_at is not None or record.expires_at < utcnow():
        raise HTTPException(status_code=401, detail="Invalid or expired sign-in code")
    record.used_at = utcnow()
    if user.status == UserStatus.pending_verify:
        user.status = UserStatus.active
        user.email_verified_at = utcnow()
    token = _ensure_token(db, user)
    user.last_login_at = utcnow()
    db.commit()
    return {
        "extension_token": token,
        "user": {"user_id": user.user_id, "email": user.email, "role": user.role.value},
    }


@router.get("/auth/me")
def medhunt_me(user: IngestUser):
    _require_recruiter(user)
    return {"user_id": user.user_id, "email": user.email, "role": user.role.value}


@router.post("/activity/enrichment")
def record_medhunt_enrichment(body: MedhuntEnrichmentEvent, user: IngestUser,
                               db: DbSession, request: Request):
    """Append one idempotent Medhunt enrichment result to HealthBoard analytics."""
    _require_recruiter(user)
    status = body.status.strip().lower()
    action = (
        MEDHUNT_ENRICHED_ACTION if status in {"found", "success"}
        else MEDHUNT_ATTEMPT_ACTION
    )
    existing = db.scalar(select(AuditLog).where(
        AuditLog.actor_user_id == user.user_id,
        AuditLog.entity_type == "medhunt_event",
        AuditLog.entity_id == body.event_id,
    ))
    if existing:
        return {"recorded": False, "event_id": body.event_id}
    forwarded = request.headers.get("x-forwarded-for", "")
    ip_address = forwarded.split(",")[0].strip()[:64] if forwarded else (
        request.client.host if request.client else None
    )
    db.add(AuditLog(
        actor_user_id=user.user_id,
        action=action,
        entity_type="medhunt_event",
        entity_id=body.event_id,
        meta={
            "candidate_id": body.candidate_id,
            "status": status,
            "source": body.source.strip().lower(),
            "run_id": body.run_id,
        },
        ip_address=ip_address,
    ))
    db.commit()
    return {"recorded": True, "event_id": body.event_id}


@router.get("/connect")
def connect(request: Request, user: CurrentUser, db: DbSession):
    """Everything the extension needs to talk to this job board as this recruiter."""
    _require_recruiter(user)
    return {
        "api_base": str(request.base_url).rstrip("/"),
        "capture_token": _ensure_token(db, user),
        "recruiter_email": user.email,
        "version": _VERSION,
        "platforms": _PLATFORMS,
    }


@router.post("/token")
def rotate_token(user: CurrentUser, db: DbSession):
    """Issue a fresh capture token (revokes the old one)."""
    _require_recruiter(user)
    user.capture_token = secrets.token_urlsafe(32)
    db.commit()
    return {"capture_token": user.capture_token}


@router.get("/download")
def download(request: Request, user: CurrentUser, db: DbSession):
    """Package the extension as a .zip, with this job board's URL baked in."""
    _require_recruiter(user)
    if not _EXT_DIR.is_dir():
        raise HTTPException(status_code=404, detail="Extension package is unavailable.")
    api_base = str(request.base_url).rstrip("/")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(_EXT_DIR.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                z.write(f, f.name)
        # Connection config the extension reads to reach this job board.
        z.writestr("jobboard-config.js",
                   "// Generated by HealthBoard — connects the extension to your job board.\n"
                   f'window.HEALTHBOARD = {{ apiBase: "{api_base}", '
                   'ingestPath: "/api/ingest/candidate", resumePath: "/api/ingest/resume" }};\n')
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="healthboard-capture-extension.zip"'},
    )
