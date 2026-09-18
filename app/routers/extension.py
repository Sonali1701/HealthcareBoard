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
from sqlalchemy import or_, select

from ..config import settings
from ..database import utcnow
from ..deps import CurrentUser, DbSession, IngestUser
from ..models import (
    AuditLog, EmailVerificationToken, Employer, EmployerMember, Notification,
    NotificationType, User, UserRole, UserStatus,
)
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


class MedhuntAssignment(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=80)
    candidate_id: str = Field(min_length=1, max_length=80)
    nexus_candidate_id: str = Field(default="", max_length=120)
    candidate_name: str = Field(default="", max_length=320)
    recruiter_user_id: str = Field(min_length=1, max_length=80)


class MedhuntMessageEvent(BaseModel):
    event_id: str = Field(min_length=8, max_length=200)
    conversation_id: str = Field(min_length=1, max_length=80)
    candidate_id: str = Field(min_length=1, max_length=80)
    nexus_candidate_id: str = Field(default="", max_length=120)
    candidate_name: str = Field(default="", max_length=320)
    initiated_by_user_id: str = Field(default="", max_length=80)
    assigned_recruiter_user_id: str = Field(default="", max_length=80)
    event_type: str = Field(min_length=1, max_length=40)
    message_preview: str = Field(default="", max_length=240)


def _user_employer(db, user_id: str) -> Employer | None:
    return db.scalar(select(Employer).where(or_(
        Employer.owner_user_id == user_id,
        Employer.employer_id.in_(select(EmployerMember.employer_id).where(
            EmployerMember.user_id == user_id
        )),
    )).limit(1))


def _employer_user_ids(db, employer: Employer) -> set[str]:
    member_ids = set(db.scalars(select(EmployerMember.user_id).where(
        EmployerMember.employer_id == employer.employer_id
    )).all())
    member_ids.add(employer.owner_user_id)
    return member_ids


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
        "detail": "If this email can use MedHunt, a sign-in code was sent.",
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


@router.get("/team/recruiters")
def medhunt_recruiters(user: IngestUser, db: DbSession):
    _require_recruiter(user)
    employer = _user_employer(db, user.user_id)
    if not employer:
        return {"items": []}
    users = db.scalars(select(User).where(
        User.user_id.in_(_employer_user_ids(db, employer)),
        User.deleted_at.is_(None),
    )).all()
    return {"items": [
        {"user_id": item.user_id, "email": item.email, "name": item.email}
        for item in sorted(users, key=lambda value: (value.email or "").lower())
        if item.role in {UserRole.recruiter, UserRole.admin}
    ]}


@router.post("/medhunt/conversations/assign")
def assign_medhunt_conversation(body: MedhuntAssignment, user: IngestUser, db: DbSession):
    _require_recruiter(user)
    employer = _user_employer(db, user.user_id)
    if not employer or body.recruiter_user_id not in _employer_user_ids(db, employer):
        raise HTTPException(status_code=403, detail="Recruiter is not on your team")
    recruiter = db.get(User, body.recruiter_user_id)
    if not recruiter or recruiter.role not in {UserRole.recruiter, UserRole.admin}:
        raise HTTPException(status_code=400, detail="Choose an active recruiter")
    event_id = hashlib.sha256(
        f"assign:{body.conversation_id}:{body.recruiter_user_id}".encode()
    ).hexdigest()[:36]
    if not db.scalar(select(AuditLog).where(
        AuditLog.action == "medhunt_conversation_assigned",
        AuditLog.entity_type == "medhunt_conversation",
        AuditLog.entity_id == event_id,
    )):
        db.add(AuditLog(
            actor_user_id=user.user_id,
            action="medhunt_conversation_assigned",
            entity_type="medhunt_conversation",
            entity_id=event_id,
            meta={
                "conversation_id": body.conversation_id,
                "candidate_id": body.candidate_id,
                "nexus_candidate_id": body.nexus_candidate_id,
                "candidate_name": body.candidate_name,
                "assigned_recruiter_user_id": recruiter.user_id,
                "source": "medhunt",
            },
        ))
        db.add(Notification(
            user_id=recruiter.user_id,
            type=NotificationType.message,
            title="Medhunt candidate reply assigned",
            body=f"{body.candidate_name or 'A candidate'} was assigned to you.",
            data={
                "source": "medhunt", "conversation_id": body.conversation_id,
                "candidate_id": body.candidate_id,
                "nexus_candidate_id": body.nexus_candidate_id,
            },
        ))
        db.commit()
    return {"user_id": recruiter.user_id, "email": recruiter.email, "name": recruiter.email}


@router.post("/medhunt/events")
def record_medhunt_message_event(body: MedhuntMessageEvent, request: Request, db: DbSession):
    supplied = request.headers.get("x-medhunt-service-token", "")
    if not settings.medhunt_service_token or not hmac.compare_digest(
        supplied, settings.medhunt_service_token
    ):
        raise HTTPException(status_code=401, detail="Invalid Medhunt service token")
    entity_id = hashlib.sha256(body.event_id.encode()).hexdigest()[:36]
    if db.scalar(select(AuditLog).where(
        AuditLog.action == f"medhunt_sms_{body.event_type}",
        AuditLog.entity_type == "medhunt_sms_event",
        AuditLog.entity_id == entity_id,
    )):
        return {"recorded": False, "event_id": body.event_id}
    recipient_id = body.assigned_recruiter_user_id or body.initiated_by_user_id
    actor_id = recipient_id if db.get(User, recipient_id) else None
    db.add(AuditLog(
        actor_user_id=actor_id,
        action=f"medhunt_sms_{body.event_type}",
        entity_type="medhunt_sms_event",
        entity_id=entity_id,
        meta={**body.model_dump(), "source": "medhunt"},
    ))
    if actor_id and body.event_type == "received":
        db.add(Notification(
            user_id=actor_id,
            type=NotificationType.message,
            title="New Medhunt candidate reply",
            body=f"{body.candidate_name or 'A candidate'} replied: {body.message_preview}"[:500],
            data={
                "source": "medhunt", "conversation_id": body.conversation_id,
                "candidate_id": body.candidate_id,
                "nexus_candidate_id": body.nexus_candidate_id,
            },
        ))
    db.commit()
    return {"recorded": True, "event_id": body.event_id}


@router.post("/activity/enrichment")
def record_medhunt_enrichment(body: MedhuntEnrichmentEvent, user: IngestUser,
                               db: DbSession, request: Request):
    """Append one idempotent MedHunt enrichment result to MedHunt analytics."""
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
                   "// Generated by MedHunt — connects the extension to your job board.\n"
                   f'window.HEALTHBOARD = {{ apiBase: "{api_base}", '
                   'ingestPath: "/api/ingest/candidate", resumePath: "/api/ingest/resume" }};\n')
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="medhunt-capture-extension.zip"'},
    )
