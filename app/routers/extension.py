"""Distribute + connect the browser capture extension.

The recruiter downloads the extension from here and connects it with a personal
capture token; the extension then POSTs candidates it captures on Indeed (and,
soon, other platforms) to /api/ingest/* as that recruiter.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import re
import secrets
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..database import utcnow
from ..deps import CurrentUser, DbSession, IngestUser
from ..models import (
    AuditLog, EmailVerificationToken, Employer, EmployerMember, Notification,
    NotificationType, User, UserRole, UserStatus,
    MedhuntCreditAccount, MedhuntCreditTransaction, MedhuntMessagingPermission,
    MedhuntSmsSender, Profile, ProfileContactBackfill,
)
from ..ratelimit import auth_rate_limit
from ..security import sha256
from ..services.email import send_medhunt_login_code
from ..services import org_roles, team_access
from ..services.notifications import notify

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
    platform: str = Field(default="", max_length=80)
    provider: str = Field(default="", max_length=80)
    run_id: str = Field(default="", max_length=120)
    occurred_at: datetime | None = None


class MedhuntServiceEnrichmentEvent(MedhuntEnrichmentEvent):
    user_id: str = Field(min_length=1, max_length=80)


class MedhuntCeipalCandidate(BaseModel):
    user_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=320)
    location: str = Field(default="", max_length=500)
    emails: list[str] = Field(default_factory=list, max_length=100)
    phones: list[str] = Field(default_factory=list, max_length=100)
    wireless_phones: list[str] = Field(default_factory=list, max_length=100)


class MedhuntLookupLimitRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=80)


class MedhuntContactBackfillResult(BaseModel):
    profile_id: str = Field(min_length=1, max_length=80)
    candidate_id: int
    status: str = Field(min_length=1, max_length=24)
    attempts: int = Field(default=0, ge=0, le=20)
    emails: list[str] = Field(default_factory=list, max_length=100)
    phones: list[str] = Field(default_factory=list, max_length=100)
    phone_contacts: list[dict] = Field(default_factory=list, max_length=100)


class MedhuntAssignment(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=80, pattern=r"^\d+$")
    candidate_id: str = Field(min_length=1, max_length=80, pattern=r"^\d+$")
    nexus_candidate_id: str = Field(default="", max_length=120)
    candidate_name: str = Field(default="", max_length=320)
    recruiter_user_id: str = Field(min_length=1, max_length=80)


def _medhunt_request(path: str, payload: dict) -> dict:
    if not settings.medhunt_api_base_url or not settings.medhunt_service_token:
        raise HTTPException(503, "Medhunt backend connection is not configured")
    try:
        response = httpx.post(
            f"{settings.medhunt_api_base_url.rstrip('/')}{path}",
            json=payload,
            headers={"X-Medhunt-Service-Token": settings.medhunt_service_token},
            timeout=15,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", "Medhunt request failed")
        except ValueError:
            detail = "Medhunt request failed"
        raise HTTPException(exc.response.status_code, detail) from exc
    except (httpx.RequestError, ValueError) as exc:
        raise HTTPException(502, "Medhunt backend is unavailable") from exc


def _medhunt_scope(db, user: User, *, employer_id: str = "", device_admin: bool = False) -> tuple[dict, Employer | None]:
    platform_admin = user.role == UserRole.admin
    if platform_admin and not employer_id:
        return {"all_users": True, "user_ids": []}, None
    employer = db.get(Employer, employer_id) if employer_id else _user_employer(db, user.user_id)
    if not employer:
        raise HTTPException(404, "Organization not found")
    role = org_roles.role_of(db, employer, user)
    allowed = {"owner", "admin"} if device_admin else {"owner", "admin", "manager"}
    if role not in allowed:
        raise HTTPException(403, "Organization admin access is required")
    member_ids = team_access.scoped_member_ids(db, employer, user)
    if not device_admin:
        # SMS conversations currently carry an initiating user, not an org ID.
        # A person in two organizations cannot be attributed safely to either.
        owners = db.execute(select(Employer.owner_user_id, Employer.employer_id).where(
            Employer.owner_user_id.in_(member_ids),
        )).all()
        memberships = db.execute(select(EmployerMember.user_id, EmployerMember.employer_id).where(
            EmployerMember.user_id.in_(member_ids),
        )).all()
        affiliations = {}
        for uid, org_id in [*owners, *memberships]:
            affiliations.setdefault(uid, set()).add(org_id)
        member_ids = {
            uid for uid in member_ids
            if affiliations.get(uid) == {employer.employer_id}
        }
    return {"all_users": False, "user_ids": sorted(member_ids)}, employer


def _normalise_sms_sender_number(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 10:
        digits = "1" + digits
    if not 8 <= len(digits) <= 15 or digits.startswith("0"):
        raise HTTPException(422, "Enter a valid international SMS sender number.")
    return "+" + digits


@router.get("/medhunt/sms-sender")
def my_medhunt_sms_sender(user: IngestUser, db: DbSession):
    _require_recruiter(user)
    organizations = team_access.active_organization_for_sender(db, user.user_id)
    organization_ids = {organization.employer_id for organization in organizations}
    senders = db.scalars(select(MedhuntSmsSender).where(
        MedhuntSmsSender.user_id == user.user_id,
        MedhuntSmsSender.employer_id.in_(organization_ids),
    )).all() if organization_ids else []
    senders = [sender for sender in senders if team_access.messaging_status(
        db, next(org for org in organizations if org.employer_id == sender.employer_id), user.user_id,
    ) == "enabled"]
    if not senders:
        raise HTTPException(403, "Your organization has not enabled Zoom messaging for your account.")
    identities = {(sender.sender_number, sender.zoom_user_id) for sender in senders}
    if len(identities) != 1:
        raise HTTPException(409, "Your organizations have different SMS sender assignments. Ask an admin to align them before sending.")
    sender = senders[0]
    return {
        "sender_number": sender.sender_number,
        "zoom_user_id": sender.zoom_user_id,
        "employer_id": sender.employer_id,
        "messaging_status": "enabled",
    }


@router.get("/medhunt/sms-senders")
def list_medhunt_sms_senders(user: CurrentUser, db: DbSession, employer_id: str = ""):
    scope, employer = _medhunt_scope(db, user, employer_id=employer_id)
    if not employer:
        raise HTTPException(400, "Choose an organization to manage SMS sender numbers.")
    ids = set(scope["user_ids"])
    rows = db.scalars(select(MedhuntSmsSender).where(
        MedhuntSmsSender.employer_id == employer.employer_id,
        MedhuntSmsSender.user_id.in_(ids),
    )).all()
    return {"items": [{
        "user_id": row.user_id,
        "sender_number": row.sender_number,
        "zoom_user_id": row.zoom_user_id,
        "messaging_status": team_access.messaging_status(db, employer, row.user_id),
    } for row in rows]}


@router.put("/medhunt/sms-senders/{target_user_id}")
def set_medhunt_sms_sender(target_user_id: str, body: MedhuntSmsSenderPatch,
                           user: CurrentUser, db: DbSession, employer_id: str = ""):
    _scope, employer = _medhunt_scope(db, user, employer_id=employer_id)
    if not employer:
        raise HTTPException(400, "Choose an organization to manage SMS sender numbers.")
    if not team_access.can_manage_member(db, employer, user, target_user_id):
        raise HTTPException(404, "That user is not a member of your organization.")
    target = db.get(User, target_user_id)
    if (not target or target.deleted_at is not None or target.status != UserStatus.active
            or target.role not in {UserRole.recruiter, UserRole.admin}):
        raise HTTPException(404, "Active recruiter organization user not found.")
    sender = db.scalar(select(MedhuntSmsSender).where(
        MedhuntSmsSender.employer_id == employer.employer_id,
        MedhuntSmsSender.user_id == target_user_id,
    ))
    number = _normalise_sms_sender_number(body.sender_number)
    zoom_user_id = body.zoom_user_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,80}", zoom_user_id):
        raise HTTPException(422, "Enter the Zoom Phone user ID associated with this number.")
    if not sender:
        sender = MedhuntSmsSender(
            employer_id=employer.employer_id, user_id=target_user_id,
            sender_number=number, zoom_user_id=zoom_user_id,
            updated_by_user_id=user.user_id,
        )
        db.add(sender)
        if not team_access.messaging_permission(db, employer.employer_id, target_user_id):
            db.add(MedhuntMessagingPermission(
                employer_id=employer.employer_id,
                user_id=target_user_id,
                status="disabled",
                reason="Zoom sender assigned; awaiting messaging approval",
                updated_by_user_id=user.user_id,
            ))
    else:
        sender.sender_number = number
        sender.zoom_user_id = zoom_user_id
        sender.updated_by_user_id = user.user_id
    db.add(AuditLog(
        actor_user_id=user.user_id, action="medhunt_sms_sender_configured",
        entity_type="user", entity_id=target_user_id,
        meta={"employer_id": employer.employer_id,
              "sender_number": number, "zoom_user_id": zoom_user_id},
    ))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "That SMS sender number is already assigned to another teammate.") from exc
    return {"user_id": target_user_id, "sender_number": number,
            "zoom_user_id": zoom_user_id,
            "messaging_status": team_access.messaging_status(db, employer, target_user_id)}


@router.get("/medhunt/devices")
def list_medhunt_devices(user: CurrentUser, db: DbSession, employer_id: str = ""):
    scope, _ = _medhunt_scope(db, user, employer_id=employer_id, device_admin=True)
    return _medhunt_request("/internal/halo/devices", scope)


@router.get("/medhunt/api-monitor")
def medhunt_api_monitor(user: CurrentUser):
    if user.role != UserRole.admin:
        raise HTTPException(403, "Platform admin access is required")
    return _medhunt_request("/internal/halo/api-monitor", {})


@router.post("/medhunt/contact-backfill-result")
def medhunt_contact_backfill_result(body: MedhuntContactBackfillResult,
                                    request: Request, db: DbSession):
    """Persist a completed shared-queue lookup into Halo's Neon profile."""
    supplied = request.headers.get("x-medhunt-service-token", "")
    if not settings.medhunt_service_token or not hmac.compare_digest(
        supplied, settings.medhunt_service_token,
    ):
        raise HTTPException(401, "Invalid Medhunt service token")
    profile = db.get(Profile, body.profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    state = db.get(ProfileContactBackfill, body.profile_id)
    if state is None:
        state = ProfileContactBackfill(profile_id=body.profile_id)
        db.add(state)

    now = utcnow()
    if body.status == "processing":
        state.status = "processing"
        state.medhunt_candidate_id = body.candidate_id
        state.attempts = max(state.attempts or 0, body.attempts)
        state.last_attempt_at = now
        state.last_error = None
        db.commit()
        return {"recorded": True, "profile_id": profile.profile_id,
                "status": "processing", "filled": []}

    emails = list(dict.fromkeys(
        value.strip()[:255] for value in body.emails if value and value.strip()
    ))
    phones = list(dict.fromkeys(
        value.strip()[:30] for value in body.phones if value and value.strip()
    ))
    mobile = next((
        str(row.get("value") or "").strip()[:30]
        for row in body.phone_contacts
        if str(row.get("kind") or "").casefold() == "mobile"
        and str(row.get("value") or "").strip()
    ), "")
    found_contact = body.status == "found" and bool(emails or phones)
    filled = []
    if found_contact:
        if not (profile.email or "").strip() and emails:
            profile.email = emails[0]
            filled.append("email")
        if not (profile.phone or "").strip() and (mobile or phones):
            profile.phone = mobile or phones[0]
            filled.append("phone")
        if filled:
            profile.contact_updated_by_email = "Quick Sourcer overnight backfill"
            profile.contact_updated_at = now
            profile.rebuild_search_text()
            from .profiles import _compute_completion
            profile.completion_score = _compute_completion(profile)
        state.status = "enriched"
        state.enriched_at = now
        state.next_eligible_at = None
        state.last_error = None
    else:
        state.status = "not_found" if body.status in {"found", "not_found"} else "failed"
        days = max(1, int(settings.contact_backfill_not_found_cooldown_days))
        state.next_eligible_at = now + (
            timedelta(days=days) if state.status == "not_found" else timedelta(minutes=15)
        )
        state.last_error = None if state.status == "not_found" else "Quick Sourcer lookup failed"
    state.medhunt_candidate_id = body.candidate_id
    state.attempts = max(state.attempts or 0, body.attempts)
    state.last_attempt_at = now
    state.result = {
        "emails": emails, "phones": phones,
        "phone_contacts": body.phone_contacts[:100], "filled": filled,
    }
    db.add(AuditLog(
        action="profile_contact_backfill_completed",
        entity_type="profile", entity_id=profile.profile_id,
        meta={"status": state.status, "filled": filled,
              "candidate_id": body.candidate_id},
    ))
    db.commit()
    return {"recorded": True, "profile_id": profile.profile_id,
            "status": state.status, "filled": filled}


@router.post("/medhunt/devices/{device_id}/approve")
def approve_medhunt_device(device_id: int, user: CurrentUser, db: DbSession,
                           employer_id: str = ""):
    scope, _ = _medhunt_scope(db, user, employer_id=employer_id, device_admin=True)
    return _medhunt_request(
        f"/internal/halo/devices/{device_id}/approve",
        {**scope, "actor_user_id": user.user_id},
    )


@router.get("/medhunt/conversations")
def list_medhunt_conversations(user: CurrentUser, db: DbSession,
                               employer_id: str = ""):
    scope, _ = _medhunt_scope(db, user, employer_id=employer_id)
    result = _medhunt_request("/internal/halo/conversations", scope)
    ids = {str(item.get(key) or "") for item in result.get("items", [])
           for key in ("initiated_by", "assigned_recruiter_id", "last_outbound_user_id")
           if item.get(key)}
    people = {person.user_id: person.email for person in db.scalars(
        select(User).where(User.user_id.in_(ids))
    ).all()} if ids else {}
    for item in result.get("items", []):
        item["initiated_by_email"] = people.get(str(item.get("initiated_by") or ""), "")
        item["assigned_recruiter_email"] = (
            item.get("assigned_recruiter_email")
            or people.get(str(item.get("assigned_recruiter_id") or ""), "")
        )
        item["last_outbound_user_email"] = people.get(
            str(item.get("last_outbound_user_id") or ""), "",
        )
    return result


@router.get("/medhunt/my-replies")
def list_my_medhunt_replies(user: CurrentUser):
    _require_recruiter(user)
    # Recruiters see only conversations they initiated or that were assigned
    # to them; managers continue to use the organization-wide reply queue.
    data = _medhunt_request(
        "/internal/halo/conversations",
        {"all_users": False, "user_ids": [user.user_id]},
    )
    return {"items": [item for item in data.get("items", []) if item.get("has_reply")]}


@router.get("/medhunt/conversations/{conversation_id}")
def get_medhunt_conversation(conversation_id: int, user: CurrentUser, db: DbSession,
                             employer_id: str = ""):
    organization = db.get(Employer, employer_id) if employer_id else _user_employer(db, user.user_id)
    organization_role = org_roles.role_of(db, organization, user) if organization else None
    if not org_roles.can(organization_role, "analytics") and user.role != UserRole.admin:
        data = _medhunt_request(
            "/internal/halo/conversations",
            {"all_users": False, "user_ids": [user.user_id]},
        )
        item = next((c for c in data.get("items", [])
                     if int(c.get("id") or 0) == conversation_id and c.get("has_reply")), None)
        if not item:
            raise HTTPException(404, "Candidate reply not found")
        conversation = _medhunt_request(
            f"/internal/halo/conversations/{conversation_id}",
            {"all_users": False, "user_ids": [user.user_id]},
        )
        conversation = _decorate_medhunt_conversation(conversation, db)
        conversation["can_reply"] = user.user_id in {
            str(conversation.get("initiated_by") or ""),
            str(conversation.get("assigned_recruiter_id") or ""),
        }
        return conversation
    scope, _ = _medhunt_scope(db, user, employer_id=employer_id)
    conversation = _medhunt_request(f"/internal/halo/conversations/{conversation_id}", scope)
    conversation = _decorate_medhunt_conversation(conversation, db)
    conversation["can_reply"] = user.user_id in {
        str(conversation.get("initiated_by") or ""),
        str(conversation.get("assigned_recruiter_id") or ""),
    }
    return conversation


def _decorate_medhunt_conversation(conversation: dict, db: DbSession) -> dict:
    ids = {str(conversation.get("initiated_by") or ""),
           str(conversation.get("assigned_recruiter_id") or "")}
    ids.update(str(message.get("sender_user_id") or "")
               for message in conversation.get("messages", []))
    ids.discard("")
    people = {person.user_id: person.email for person in db.scalars(
        select(User).where(User.user_id.in_(ids))
    ).all()} if ids else {}
    conversation["initiated_by_email"] = people.get(str(conversation.get("initiated_by") or ""), "")
    for message in conversation.get("messages", []):
        if message.get("sender_user_id"):
            message["sender_name"] = people.get(str(message["sender_user_id"]), "Team member")
    return conversation


@router.post("/medhunt/conversations/{conversation_id}/reply")
def reply_to_medhunt_conversation(conversation_id: int, body: MedhuntConversationReply,
                                  user: CurrentUser, db: DbSession,
                                  employer_id: str = ""):
    organization = db.get(Employer, employer_id) if employer_id else _user_employer(db, user.user_id)
    organization_role = org_roles.role_of(db, organization, user) if organization else None
    if not org_roles.can(organization_role, "analytics") and user.role != UserRole.admin:
        scope = {"all_users": False, "user_ids": [user.user_id]}
        employer = organization
    else:
        scope, employer = _medhunt_scope(db, user, employer_id=employer_id)
    if not employer or user.user_id not in team_access.organization_member_ids(db, employer):
        raise HTTPException(403, "An organization member account is required to reply.")
    conversation = _medhunt_request(
        f"/internal/halo/conversations/{conversation_id}", scope,
    )
    if conversation.get("status") == "opted_out":
        raise HTTPException(409, "This candidate opted out and cannot be messaged.")
    if not any(m.get("direction") == "inbound" for m in conversation.get("messages", [])):
        raise HTTPException(409, "Wait for a candidate reply before replying.")
    sender = db.scalar(select(MedhuntSmsSender).where(
        MedhuntSmsSender.employer_id == employer.employer_id,
        MedhuntSmsSender.user_id == user.user_id,
    ))
    if not sender:
        raise HTTPException(409, "Ask your organization admin to configure your SMS sender number.")
    if team_access.messaging_status(db, employer, user.user_id) != "enabled":
        raise HTTPException(403, "Your organization has paused or disabled your messaging access.")
    text = body.message.strip()
    if not text:
        raise HTTPException(422, "Reply cannot be blank.")
    result = _medhunt_request(
        f"/internal/halo/conversations/{conversation_id}/reply",
        {**scope, "actor_user_id": user.user_id, "actor_name": user.email, "message": text,
         "sender_number": sender.sender_number, "zoom_user_id": sender.zoom_user_id,
         "request_id": body.request_id or secrets.token_urlsafe(24)},
    )
    return _decorate_medhunt_conversation(result, db)


@router.get("/medhunt/conversations/{conversation_id}/recruiters")
def recruiters_for_medhunt_conversation(conversation_id: int, user: CurrentUser,
                                        db: DbSession, employer_id: str = ""):
    scope, scoped_employer = _medhunt_scope(db, user, employer_id=employer_id)
    conversation = _medhunt_request(
        f"/internal/halo/conversations/{conversation_id}", scope,
    )
    employer = scoped_employer or _user_employer(
        db, str(conversation.get("initiated_by") or ""),
    )
    if not employer:
        return {"items": []}
    visible_ids = set(scope.get("user_ids") or [])
    users = db.scalars(select(User).where(
        User.user_id.in_(visible_ids),
        User.deleted_at.is_(None), User.status == UserStatus.active,
    )).all()
    return {"items": [
        {"user_id": item.user_id, "email": item.email,
         "name": item.email}
        for item in users if item.role in {UserRole.recruiter, UserRole.admin}
    ]}


class MedhuntMessageEvent(BaseModel):
    event_id: str = Field(min_length=8, max_length=200)
    conversation_id: str = Field(min_length=1, max_length=80)
    candidate_id: str = Field(min_length=1, max_length=80)
    nexus_candidate_id: str = Field(default="", max_length=120)
    candidate_name: str = Field(default="", max_length=320)
    initiated_by_user_id: str = Field(default="", max_length=80)
    assigned_recruiter_user_id: str = Field(default="", max_length=80)
    sender_user_id: str = Field(default="", max_length=80)
    event_type: str = Field(min_length=1, max_length=40)
    message_preview: str = Field(default="", max_length=240)


class MedhuntConversationReply(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    request_id: str = Field(default="", max_length=120)


class MedhuntCreditConsume(BaseModel):
    candidate_ids: list[int] = Field(min_length=1, max_length=100)
    run_id: str = Field(default="", max_length=120)


class MedhuntCreditGrant(BaseModel):
    amount: int = Field(gt=0, le=10000)
    note: str = Field(default="", max_length=300)


class MedhuntCreditBulkAdjust(BaseModel):
    user_ids: list[str] = Field(min_length=1, max_length=500)
    amount: int = Field(ge=-10000, le=10000)
    note: str = Field(default="", max_length=300)


class MedhuntCreditBulkSet(BaseModel):
    user_ids: list[str] = Field(min_length=1, max_length=500)
    balance: int = Field(ge=0, le=10000)
    note: str = Field(default="", max_length=300)


class MedhuntSmsSenderPatch(BaseModel):
    sender_number: str = Field(min_length=7, max_length=40)
    zoom_user_id: str = Field(min_length=1, max_length=80)


class MedhuntMessagingPermissionPatch(BaseModel):
    status: str = Field(pattern=r"^(enabled|paused|disabled)$")
    reason: str = Field(default="", max_length=500)


class MedhuntZoomAccessRequest(BaseModel):
    employer_id: str = Field(min_length=1, max_length=80)


class MedhuntAtsConfigurationRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=80)
    provider: str = Field(pattern=r"^(ceipal|nexus)$")


@router.post("/medhunt/zoom-access")
def medhunt_zoom_access(body: MedhuntZoomAccessRequest, request: Request, db: DbSession):
    supplied = request.headers.get("x-medhunt-service-token", "")
    if not settings.medhunt_service_token or not hmac.compare_digest(
        supplied, settings.medhunt_service_token,
    ):
        raise HTTPException(401, "Invalid Medhunt service token")
    from ..models import ZoomOrganizationIntegration
    from ..services import zoom_oauth

    integration = db.scalar(select(ZoomOrganizationIntegration).where(
        ZoomOrganizationIntegration.employer_id == body.employer_id,
    ))
    if not integration:
        raise HTTPException(409, "This organization has not connected Zoom.")
    return {"access_token": zoom_oauth.access_token(db, integration),
            "employer_id": body.employer_id}


@router.post("/medhunt/ats-configuration")
def medhunt_ats_configuration(body: MedhuntAtsConfigurationRequest,
                              request: Request, db: DbSession):
    """Return one organization's decrypted ATS settings over the service channel."""
    supplied = request.headers.get("x-medhunt-service-token", "")
    if not settings.medhunt_service_token or not hmac.compare_digest(
        supplied, settings.medhunt_service_token,
    ):
        raise HTTPException(401, "Invalid Medhunt service token")
    user = db.get(User, body.user_id)
    if not user or user.deleted_at is not None:
        raise HTTPException(404, "Medhunt user not found")
    from ..services import ats_connections

    resolved = ats_connections.for_user(
        db, user.user_id, body.provider, connected_only=False,
    )
    if not resolved:
        return {"configured": False, "managed": False, "provider": body.provider}
    employer, integration = resolved
    if integration.status != "connected":
        return {
            "configured": False, "managed": True,
            "provider": body.provider, "employer_id": employer.employer_id,
        }
    return {
        "configured": True, "managed": True,
        "provider": body.provider,
        "employer_id": employer.employer_id,
        "settings": ats_connections.settings(integration),
    }


@router.patch("/medhunt/messaging-permissions/{target_user_id}")
def set_medhunt_messaging_permission(
    target_user_id: str,
    body: MedhuntMessagingPermissionPatch,
    user: CurrentUser,
    db: DbSession,
    employer_id: str = "",
):
    _scope, employer = _medhunt_scope(db, user, employer_id=employer_id)
    if not employer:
        raise HTTPException(400, "Choose an organization to manage messaging access.")
    if not team_access.can_manage_member(db, employer, user, target_user_id):
        raise HTTPException(403, "You can manage messaging only for members of your teams.")
    target = db.get(User, target_user_id)
    if not target or target.deleted_at is not None or target.status != UserStatus.active:
        raise HTTPException(404, "Choose an active organization member.")
    if body.status == "enabled" and not db.scalar(select(MedhuntSmsSender.sender_id).where(
        MedhuntSmsSender.employer_id == employer.employer_id,
        MedhuntSmsSender.user_id == target_user_id,
    )):
        raise HTTPException(409, "Sync or assign a Zoom Phone sender before enabling messaging.")
    permission = team_access.messaging_permission(db, employer.employer_id, target_user_id)
    previous = permission.status if permission else team_access.messaging_status(db, employer, target_user_id)
    if not permission:
        permission = MedhuntMessagingPermission(
            employer_id=employer.employer_id,
            user_id=target_user_id,
            status=body.status,
            reason=body.reason.strip() or None,
            updated_by_user_id=user.user_id,
        )
        db.add(permission)
    else:
        permission.status = body.status
        permission.reason = body.reason.strip() or None
        permission.updated_by_user_id = user.user_id
        permission.permission_version = int(permission.permission_version or 0) + 1
    db.add(AuditLog(
        actor_user_id=user.user_id,
        action=f"medhunt_messaging_{body.status}",
        entity_type="user",
        entity_id=target_user_id,
        meta={
            "employer_id": employer.employer_id,
            "previous_status": previous,
            "status": body.status,
            "reason": body.reason.strip(),
        },
    ))
    db.commit()
    return {
        "user_id": target_user_id,
        "status": permission.status,
        "permission_version": permission.permission_version,
    }


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


def _ensure_medhunt_credit_account(db, user_id: str) -> MedhuntCreditAccount:
    account = db.scalar(select(MedhuntCreditAccount).where(
        MedhuntCreditAccount.user_id == user_id,
    ))
    if account:
        return account
    account = MedhuntCreditAccount(
        user_id=user_id, balance=100, lifetime_granted=100, lifetime_spent=0,
    )
    db.add(account)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        account = db.scalar(select(MedhuntCreditAccount).where(
            MedhuntCreditAccount.user_id == user_id,
        ))
        if account:
            return account
        raise
    db.add(MedhuntCreditTransaction(
        account_id=account.account_id, user_id=user_id, delta=100,
        balance_after=100, reason="signup_bonus", note="Starting Medhunt enrichment credits",
    ))
    return account


def _medhunt_credit_summary(db, user_id: str) -> dict:
    account = _ensure_medhunt_credit_account(db, user_id)
    return {
        "balance": int(account.balance),
        "lifetime_granted": int(account.lifetime_granted),
        "lifetime_spent": int(account.lifetime_spent),
    }


@router.get("/medhunt/credits")
def my_medhunt_credits(user: IngestUser, db: DbSession):
    _require_recruiter(user)
    summary = _medhunt_credit_summary(db, user.user_id)
    db.commit()
    return summary


@router.post("/medhunt/credits/consume")
def consume_medhunt_credits(body: MedhuntCreditConsume, user: IngestUser,
                            db: DbSession):
    """Atomically spend one extension credit per distinct candidate, once per user."""
    _require_recruiter(user)
    candidate_ids = list(dict.fromkeys(int(cid) for cid in body.candidate_ids if int(cid) > 0))
    if not candidate_ids:
        raise HTTPException(422, "At least one valid candidate ID is required.")
    account = _ensure_medhunt_credit_account(db, user.user_id)
    # Serialize balance checks for this user. The idempotency ledger then makes
    # retries and overlapping concurrent batches charge only once per candidate.
    db.scalar(select(MedhuntCreditAccount).where(
        MedhuntCreditAccount.user_id == user.user_id,
    ).with_for_update())
    keys = {
        candidate_id: f"medhunt-enrichment:{user.user_id}:{candidate_id}"
        for candidate_id in candidate_ids
    }
    already = set(db.scalars(select(MedhuntCreditTransaction.idempotency_key).where(
        MedhuntCreditTransaction.idempotency_key.in_(list(keys.values())),
    )).all())
    charge_ids = [cid for cid, key in keys.items() if key not in already]
    if charge_ids:
        changed = db.execute(text(
            "UPDATE medhunt_credit_accounts SET balance = balance - :n, "
            "lifetime_spent = lifetime_spent + :n, updated_at = :now "
            "WHERE user_id = :uid AND balance >= :n"
        ), {"n": len(charge_ids), "now": utcnow(), "uid": user.user_id}).rowcount
        if not changed:
            db.refresh(account)
            raise HTTPException(
                402,
                f"Not enough extension credits: need {len(charge_ids)}, "
                f"you have {account.balance}. Ask your organization admin or manager for more.",
            )
        db.refresh(account)
        for index, candidate_id in enumerate(charge_ids):
            db.add(MedhuntCreditTransaction(
                account_id=account.account_id, user_id=user.user_id,
                delta=-1,
                balance_after=account.balance + len(charge_ids) - index - 1,
                reason="enrichment_lookup", idempotency_key=keys[candidate_id],
                note=f"Candidate {candidate_id} enrichment lookup",
            ))
    db.commit()
    return {
        "balance": int(account.balance),
        "charged": len(charge_ids),
        "already_charged": len(candidate_ids) - len(charge_ids),
    }


@router.get("/medhunt/credits/team")
def list_medhunt_team_credits(user: CurrentUser, db: DbSession,
                              employer_id: str = ""):
    scope, employer = _medhunt_scope(db, user, employer_id=employer_id)
    if not employer:
        raise HTTPException(400, "Choose an organization to manage extension credits.")
    role = org_roles.role_of(db, employer, user)
    if not org_roles.can(role, "medhunt_credits"):
        raise HTTPException(403, "You cannot manage Medhunt extension credits.")
    member_ids = set(scope.get("user_ids") or [])
    members = db.scalars(select(User).where(
        User.user_id.in_(member_ids), User.deleted_at.is_(None),
    )).all()
    items = []
    for member in members:
        items.append({
            "user_id": member.user_id,
            "email": member.email,
            "balance": _medhunt_credit_summary(db, member.user_id)["balance"],
        })
    db.commit()
    return {"items": sorted(items, key=lambda item: (item["email"] or "").lower())}


@router.post("/medhunt/credits/team/{target_user_id}/grant")
def grant_medhunt_team_credits(target_user_id: str, body: MedhuntCreditGrant,
                               user: CurrentUser, db: DbSession,
                               employer_id: str = ""):
    _scope, employer = _medhunt_scope(db, user, employer_id=employer_id)
    if not employer:
        raise HTTPException(400, "Choose an organization to manage extension credits.")
    role = org_roles.role_of(db, employer, user)
    if not org_roles.can(role, "medhunt_credits"):
        raise HTTPException(403, "You cannot manage Medhunt extension credits.")
    if target_user_id not in set(_scope.get("user_ids") or []):
        raise HTTPException(404, "That user is not a member of your organization.")
    if role == "manager" and not team_access.can_manage_member(
        db, employer, user, target_user_id,
    ):
        raise HTTPException(403, "Managers can grant credits only to members of their teams.")
    target = db.get(User, target_user_id)
    if not target or target.deleted_at is not None:
        raise HTTPException(404, "User not found.")
    account = _ensure_medhunt_credit_account(db, target_user_id)
    db.execute(text(
        "UPDATE medhunt_credit_accounts SET balance = balance + :n, "
        "lifetime_granted = lifetime_granted + :n, updated_at = :now "
        "WHERE user_id = :uid"
    ), {"n": body.amount, "now": utcnow(), "uid": target_user_id})
    db.refresh(account)
    db.add(MedhuntCreditTransaction(
        account_id=account.account_id, user_id=target_user_id,
        delta=body.amount, balance_after=account.balance, reason="manager_grant",
        note=body.note.strip() or f"Granted by {user.email}", actor_user_id=user.user_id,
    ))
    db.add(AuditLog(
        actor_user_id=user.user_id, action="medhunt_extension_credits_granted",
        entity_type="user", entity_id=target_user_id,
        meta={"amount": body.amount, "balance": account.balance,
              "employer_id": employer.employer_id, "note": body.note.strip()},
    ))
    db.commit()
    return {"balance": int(account.balance), "granted": body.amount}


@router.post("/medhunt/credits/team/bulk-adjust")
def adjust_medhunt_team_credits_bulk(body: MedhuntCreditBulkAdjust,
                                     user: CurrentUser, db: DbSession,
                                     employer_id: str = ""):
    """Add or remove extension credits for selected team members as one batch."""
    return _apply_medhunt_team_credit_bulk(
        user_ids=body.user_ids, amount=body.amount, note=body.note,
        user=user, db=db, employer_id=employer_id, set_balance=False,
    )


@router.post("/medhunt/credits/team/bulk-set")
def set_medhunt_team_credits_bulk(body: MedhuntCreditBulkSet,
                                  user: CurrentUser, db: DbSession,
                                  employer_id: str = ""):
    """Set the selected members' extension-credit balances to an exact value."""
    return _apply_medhunt_team_credit_bulk(
        user_ids=body.user_ids, amount=body.balance, note=body.note,
        user=user, db=db, employer_id=employer_id, set_balance=True,
    )


def _apply_medhunt_team_credit_bulk(*, user_ids: list[str], amount: int,
                                    note: str, user: CurrentUser, db: DbSession,
                                    employer_id: str, set_balance: bool):
    _scope, employer = _medhunt_scope(db, user, employer_id=employer_id)
    if not employer:
        raise HTTPException(400, "Choose an organization to manage extension credits.")
    role = org_roles.role_of(db, employer, user)
    if role not in {"owner", "admin"}:
        raise HTTPException(403, "Only organization admins can adjust credits in bulk.")
    if amount == 0 and not set_balance:
        raise HTTPException(422, "Enter a non-zero credit adjustment.")

    requested_ids = list(dict.fromkeys(str(value).strip() for value in user_ids if str(value).strip()))
    if not requested_ids:
        raise HTTPException(422, "Select at least one team member.")
    member_ids = _employer_user_ids(db, employer)
    outside_org = sorted(set(requested_ids) - member_ids)
    if outside_org:
        raise HTTPException(404, "One or more selected users are not members of this organization.")
    members = db.scalars(select(User).where(
        User.user_id.in_(requested_ids), User.deleted_at.is_(None),
    )).all()
    if len(members) != len(requested_ids):
        raise HTTPException(404, "One or more selected users are unavailable.")

    # Initialize missing accounts before taking locks, then serialize balances
    # in a stable order so concurrent admin changes cannot overwrite each other.
    for target_id in sorted(requested_ids):
        _ensure_medhunt_credit_account(db, target_id)
    accounts = db.scalars(select(MedhuntCreditAccount).where(
        MedhuntCreditAccount.user_id.in_(requested_ids),
    ).order_by(MedhuntCreditAccount.user_id).with_for_update()).all()
    note = note.strip() or f"Bulk {'balance set' if set_balance else 'adjustment'} by {user.email}"
    results = []
    for account in accounts:
        old_balance = int(account.balance)
        new_balance = amount if set_balance else max(0, old_balance + amount)
        actual_delta = new_balance - old_balance
        account.balance = new_balance
        if actual_delta > 0:
            account.lifetime_granted = int(account.lifetime_granted or 0) + actual_delta
        target = next(member for member in members if member.user_id == account.user_id)
        if actual_delta:
            db.add(MedhuntCreditTransaction(
                account_id=account.account_id, user_id=account.user_id,
                delta=actual_delta, balance_after=new_balance,
                reason="admin_bulk_set" if set_balance else "admin_bulk_adjustment",
                note=note,
                actor_user_id=user.user_id,
            ))
        results.append({
            "user_id": account.user_id,
            "email": target.email,
            "balance": new_balance,
            "adjusted": actual_delta,
        })

    db.add(AuditLog(
        actor_user_id=user.user_id,
        action="medhunt_extension_credits_bulk_set" if set_balance
            else "medhunt_extension_credits_bulk_adjusted",
        entity_type="organization", entity_id=employer.employer_id,
        meta={
            "operation": "set" if set_balance else "adjust",
            "requested_amount": amount,
            "user_ids": requested_ids,
            "note": note,
            "results": results,
        },
    ))
    db.commit()
    return {"items": results, "requested_amount": amount,
            "operation": "set" if set_balance else "adjust"}


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
    return {
        "user_id": user.user_id,
        "email": user.email,
        "role": user.role.value,
        "delivery_targets": {
            "ceipal": bool(user.medhunt_ceipal_enabled),
            "nexus": bool(user.medhunt_nexus_enabled),
        },
    }


@router.post("/medhunt/ceipal-candidate")
def medhunt_ceipal_candidate(body: MedhuntCeipalCandidate, request: Request, db: DbSession):
    supplied = request.headers.get("x-medhunt-service-token", "")
    if not settings.medhunt_service_token or not hmac.compare_digest(
        supplied, settings.medhunt_service_token
    ):
        raise HTTPException(401, "Invalid Medhunt service token")
    user = db.get(User, body.user_id)
    if not user or user.deleted_at is not None:
        raise HTTPException(404, "Medhunt user not found")
    if not user.medhunt_ceipal_enabled:
        raise HTTPException(403, "This recruiter is not assigned to Ceipal")
    from ..services import ats_connections
    from ..services.medhunt_ceipal import MedhuntCeipalError, upload_candidate
    try:
        resolved = ats_connections.for_user(
            db, user.user_id, "ceipal", connected_only=False,
        )
        if resolved and resolved[1].status != "connected":
            raise MedhuntCeipalError("This organization's CEIPAL connection is disconnected.")
        configuration = ats_connections.settings(resolved[1]) if resolved else None
        result = upload_candidate(body.model_dump(), configuration)
    except MedhuntCeipalError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "Ceipal candidate processing is temporarily unavailable") from exc
    return result


@router.post("/medhunt/contact-lookup-limit")
def medhunt_contact_lookup_limit(body: MedhuntLookupLimitRequest, request: Request, db: DbSession):
    supplied = request.headers.get("x-medhunt-service-token", "")
    if not settings.medhunt_service_token or not hmac.compare_digest(
        supplied, settings.medhunt_service_token
    ):
        raise HTTPException(401, "Invalid Medhunt service token")
    user = db.get(User, body.user_id)
    if not user or user.deleted_at is not None:
        raise HTTPException(404, "Medhunt user not found")
    employer_ids = set(db.scalars(select(Employer.employer_id).where(
        Employer.owner_user_id == user.user_id,
    )).all())
    employer_ids.update(db.scalars(select(EmployerMember.employer_id).where(
        EmployerMember.user_id == user.user_id,
    )).all())
    employers = db.scalars(select(Employer).where(
        Employer.employer_id.in_(employer_ids),
    )).all() if employer_ids else []
    memberships = db.scalars(select(EmployerMember).where(
        EmployerMember.user_id == user.user_id,
        EmployerMember.employer_id.in_(employer_ids),
    )).all() if employer_ids else []
    member_limits = {
        membership.employer_id: membership.quick_sourcer_limit_override
        for membership in memberships
    }
    # The extension identifies a recruiter, not an active org. Resolve each
    # organization's member override (or org default), then use the most
    # restrictive effective limit if the recruiter belongs to several orgs.
    limit = min(
        (
            int(
                member_limits.get(employer.employer_id)
                if member_limits.get(employer.employer_id) is not None
                else employer.quick_sourcer_per_user_limit or 10
            )
            for employer in employers
        ),
        default=10,
    )
    return {"user_id": user.user_id, "per_user_limit": max(1, min(80, limit))}


@router.get("/team/recruiters")
def medhunt_recruiters(user: IngestUser, db: DbSession):
    _require_recruiter(user)
    employer = _user_employer(db, user.user_id)
    if not employer:
        return {"items": []}
    users = db.scalars(select(User).where(
        User.user_id.in_(team_access.scoped_member_ids(db, employer, user)),
        User.deleted_at.is_(None),
    )).all()
    return {"items": [
        {"user_id": item.user_id, "email": item.email, "name": item.email}
        for item in sorted(users, key=lambda value: (value.email or "").lower())
        if item.role in {UserRole.recruiter, UserRole.admin}
    ]}


@router.post("/medhunt/conversations/assign")
def assign_medhunt_conversation(body: MedhuntAssignment, user: CurrentUser, db: DbSession,
                                employer_id: str = ""):
    scope, scoped_employer = _medhunt_scope(db, user, employer_id=employer_id)
    conversation = _medhunt_request(
        f"/internal/halo/conversations/{body.conversation_id}", scope,
    )
    if str(conversation.get("candidate_id")) != body.candidate_id:
        raise HTTPException(409, "Candidate and conversation do not match")
    if not any(message.get("direction") == "inbound"
               for message in conversation.get("messages", [])):
        raise HTTPException(409, "Wait for a candidate reply before assigning")
    latest_inbound = max((float(message.get("created") or 0)
                          for message in conversation.get("messages", [])
                          if message.get("direction") == "inbound"), default=0)
    latest_outbound = max((float(message.get("created") or 0)
                           for message in conversation.get("messages", [])
                           if message.get("direction") == "outbound"
                           and message.get("status") in {"accepted", "sent", "delivered"}), default=0)
    if latest_outbound > latest_inbound:
        raise HTTPException(409, "This conversation has already been answered; reassignment is no longer available.")
    if conversation.get("status") == "opted_out":
        raise HTTPException(409, "An opted-out candidate cannot be reassigned for outreach.")
    employer = scoped_employer or _user_employer(
        db, str(conversation.get("initiated_by") or ""),
    )
    if not employer or body.recruiter_user_id not in set(scope.get("user_ids") or []):
        raise HTTPException(status_code=403, detail="Recruiter is not on your team")
    recruiter = db.get(User, body.recruiter_user_id)
    if not recruiter or recruiter.deleted_at is not None or recruiter.status != UserStatus.active \
            or recruiter.role not in {UserRole.recruiter, UserRole.admin}:
        raise HTTPException(status_code=400, detail="Choose an active recruiter")
    if str(conversation.get("assigned_recruiter_id") or "") != recruiter.user_id:
        _medhunt_request(
            f"/internal/halo/conversations/{body.conversation_id}/assign",
            {**scope, "actor_user_id": user.user_id,
             "recruiter_user_id": recruiter.user_id,
             "recruiter_email": recruiter.email,
             "recruiter_name": recruiter.email},
        )
        db.add(AuditLog(
            actor_user_id=user.user_id,
            action="medhunt_conversation_assigned",
            entity_type="medhunt_conversation",
            entity_id=secrets.token_hex(18),
            meta={
                "conversation_id": body.conversation_id,
                "candidate_id": body.candidate_id,
                "nexus_candidate_id": str(conversation.get("nexus_candidate_id") or ""),
                "candidate_name": str(conversation.get("candidate_name") or ""),
                "assigned_recruiter_user_id": recruiter.user_id,
                "source": "medhunt",
            },
        ))
        notify(
            db, user_id=recruiter.user_id,
            type=NotificationType.message,
            title="Medhunt candidate reply assigned",
            body=f"{conversation.get('candidate_name') or 'A candidate'} was assigned to you.",
            data={
                "source": "medhunt", "conversation_id": body.conversation_id,
                "candidate_id": body.candidate_id,
                "nexus_candidate_id": str(conversation.get("nexus_candidate_id") or ""),
            },
            email=True,
        )
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
    actor_id = body.sender_user_id or recipient_id
    actor_id = actor_id if db.get(User, actor_id) else None
    db.add(AuditLog(
        actor_user_id=actor_id,
        action=f"medhunt_sms_{body.event_type}",
        entity_type="medhunt_sms_event",
        entity_id=entity_id,
        meta={**body.model_dump(), "source": "medhunt"},
    ))
    if actor_id and body.event_type == "received":
        notify(
            db, user_id=actor_id,
            type=NotificationType.message,
            title="New Medhunt candidate reply",
            body=f"{body.candidate_name or 'A candidate'} replied. Open Halo to review the message.",
            data={
                "source": "medhunt", "conversation_id": body.conversation_id,
                "candidate_id": body.candidate_id,
                "nexus_candidate_id": body.nexus_candidate_id,
            },
            email=True,
        )
    db.commit()
    return {"recorded": True, "event_id": body.event_id}


@router.post("/activity/enrichment")
def record_medhunt_enrichment(body: MedhuntEnrichmentEvent, user: IngestUser,
                               db: DbSession, request: Request):
    """Append one idempotent MedHunt enrichment result to MedHunt analytics."""
    _require_recruiter(user)
    return _save_medhunt_enrichment(body, user, db, request)


@router.post("/activity/enrichment/service")
def record_medhunt_enrichment_service(body: MedhuntServiceEnrichmentEvent,
                                      db: DbSession, request: Request):
    supplied = request.headers.get("x-medhunt-service-token", "")
    if not settings.medhunt_service_token or not hmac.compare_digest(
        supplied, settings.medhunt_service_token
    ):
        raise HTTPException(401, "Invalid Medhunt service token")
    user = db.get(User, body.user_id)
    if not user or user.deleted_at is not None:
        raise HTTPException(404, "Medhunt event user not found")
    return _save_medhunt_enrichment(body, user, db, request)


def _save_medhunt_enrichment(body: MedhuntEnrichmentEvent, user: User,
                             db: DbSession, request: Request):
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
    occurred_at = body.occurred_at
    if occurred_at is not None:
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        occurred_at = occurred_at.astimezone(timezone.utc)
        if occurred_at > utcnow() + timedelta(minutes=5):
            raise HTTPException(422, "Event time cannot be in the future")
    platform = (body.platform or body.source).strip().lower()
    db.add(AuditLog(
        actor_user_id=user.user_id,
        action=action,
        entity_type="medhunt_event",
        entity_id=body.event_id,
        meta={
            "candidate_id": body.candidate_id,
            "status": status,
            "source": body.source.strip().lower(),
            "platform": platform,
            "provider": body.provider.strip().lower(),
            "run_id": body.run_id,
        },
        ip_address=ip_address,
        **({"created_at": occurred_at} if occurred_at else {}),
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
