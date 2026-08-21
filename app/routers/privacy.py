"""Privacy controls for the people listed in the provider directory.

Nearly everyone in this directory arrived from an imported résumé: they never
signed up, and their contact details are sold to recruiters a credit at a time.
That makes access, correction and deletion something the platform has to be
able to honour on request, not a nice-to-have.

Two routes in, because almost nobody listed here has an account:

* A signed-in professional can delist or export their own record directly.
* Anyone else asks by email address. That request is NOT actioned immediately —
  it issues a single-use token and mails it, so one person cannot delist
  another by typing their address. Only confirming the token delists.

Delisting hides the profile and wipes the contact details recruiters bought
access to; the row itself is retained (screen_reason = "opted_out") so the
same résumé cannot simply be re-imported tomorrow and reappear.
"""
from __future__ import annotations

import secrets
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select

from ..config import settings
from ..database import utcnow
from ..deps import CurrentUser, DbSession
from ..models import AuditLog, Profile, Session, User, UserStatus
from ..security import verify_password
from ..services.email import send_email

router = APIRouter(prefix="/api/privacy", tags=["privacy"])

OPT_OUT_REASON = "opted_out"
ACTION_DELIST = "profile_delisted"
ACTION_RELIST = "profile_relisted"
ACTION_EXPORT = "profile_exported"
ACTION_REQUEST = "opt_out_requested"

# Contact details are the thing recruiters pay for, so an opt-out has to remove
# them, not merely hide the row.
_CONTACT_FIELDS = ("email", "phone", "resume_url")


class OptOutRequest(BaseModel):
    email: EmailStr


class OptOutConfirm(BaseModel):
    token: str = Field(min_length=10)


class AccountDeleteRequest(BaseModel):
    # Deleting an account is destructive, so confirm the password first.
    password: str


def _log(db: DbSession, action: str, profile: Profile, *, actor: Optional[str] = None,
         request: Optional[Request] = None, meta: Optional[dict] = None) -> None:
    db.add(AuditLog(
        actor_user_id=actor,
        action=action,
        entity_type="profile",
        entity_id=profile.profile_id,
        meta=meta or {},
        ip_address=request.client.host if request and request.client else None,
    ))


def _delist(db: DbSession, profile: Profile, *, actor: Optional[str],
            request: Optional[Request], via: str) -> None:
    removed = [f for f in _CONTACT_FIELDS if getattr(profile, f, None)]
    for field in _CONTACT_FIELDS:
        setattr(profile, field, None)
    profile.is_listable = False
    profile.screen_reason = OPT_OUT_REASON
    profile.screened_at = utcnow()
    profile.open_to_work = False
    _log(db, ACTION_DELIST, profile, actor=actor, request=request,
         meta={"via": via, "contact_removed": removed})


# --- Signed-in professional ------------------------------------------------

@router.get("/me/export")
def export_my_data(user: CurrentUser, db: DbSession, request: Request):
    """Everything the platform holds about this person, for a data request."""
    from ..models import Application, Certification, License, WorkHistory

    profile = db.scalar(select(Profile).where(Profile.user_id == user.user_id))
    if not profile:
        raise HTTPException(status_code=404, detail="No profile for this account")

    def rows(model, *fields):
        return [{f: getattr(r, f) for f in fields}
                for r in db.scalars(select(model).where(
                    model.profile_id == profile.profile_id)).all()]

    _log(db, ACTION_EXPORT, profile, actor=user.user_id, request=request)
    db.commit()
    return {
        "account": {"email": user.email, "role": user.role.value,
                    "created_at": user.created_at},
        "profile": {c.name: getattr(profile, c.name)
                    for c in Profile.__table__.columns},
        "licenses": rows(License, "license_type", "state_code", "license_number",
                         "expiry_date", "is_compact"),
        "certifications": rows(Certification, "cert_name", "expiry_date"),
        "work_history": rows(WorkHistory, "employer_name", "job_title",
                             "start_date", "end_date"),
        "applications": rows(Application, "job_id", "status", "applied_at"),
        "listed_in_directory": bool(profile.is_listable),
        "note": ("Recruiters only ever see your name and contact details after "
                 "deliberately releasing your profile, which is audit-logged."),
    }


@router.post("/me/delist")
def delist_me(user: CurrentUser, db: DbSession, request: Request):
    """Remove yourself from the recruiter directory and erase contact details."""
    profile = db.scalar(select(Profile).where(Profile.user_id == user.user_id))
    if not profile:
        raise HTTPException(status_code=404, detail="No profile for this account")
    _delist(db, profile, actor=user.user_id, request=request, via="self_service")
    db.commit()
    return {"listed": False,
            "message": "You have been removed from the recruiter directory and "
                       "your contact details erased."}


@router.post("/me/relist")
def relist_me(user: CurrentUser, db: DbSession, request: Request):
    """Opt back in. Contact details are not restored — they were erased."""
    profile = db.scalar(select(Profile).where(Profile.user_id == user.user_id))
    if not profile:
        raise HTTPException(status_code=404, detail="No profile for this account")
    profile.is_listable = True
    if profile.screen_reason == OPT_OUT_REASON:
        profile.screen_reason = None
    _log(db, ACTION_RELIST, profile, actor=user.user_id, request=request)
    db.commit()
    return {"listed": True,
            "message": "You are listed again. Add your contact details back so "
                       "recruiters can reach you."}


@router.post("/me/delete")
def delete_my_account(body: AccountDeleteRequest, user: CurrentUser, db: DbSession,
                      request: Request):
    """Permanently delete the signed-in account and erase personal details.

    Deletion goes further than delisting: the account is closed (and can no
    longer sign in), every session is revoked, the login email is scrubbed, and
    any directory profile is delisted AND anonymised (name removed), not just
    hidden. Consistent with the module's stance, the profile row is retained in
    anonymised form so the same résumé cannot be re-imported and reappear.
    """
    if not user.password_hash or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Password is incorrect")

    profile = db.scalar(select(Profile).where(Profile.user_id == user.user_id))
    if profile:
        _delist(db, profile, actor=user.user_id, request=request, via="account_deletion")
        profile.first_name, profile.last_name = "Deleted", "User"
        profile.headline = None
        profile.bio = None
        profile.rebuild_search_text()

    # Close the account and remove its credentials / identifiers. The email is
    # replaced with a unique tombstone so the address is freed and the unique
    # index still holds.
    user.deleted_at = utcnow()
    user.status = UserStatus.deleted
    user.email = f"deleted+{user.user_id}@deleted.invalid"
    user.password_hash = None
    user.mfa_enabled = False
    user.mfa_secret = None
    user.capture_token = None
    for s in db.scalars(select(Session).where(Session.user_id == user.user_id)):
        s.revoked_at = utcnow()

    db.add(AuditLog(actor_user_id=user.user_id, action="account_deleted",
                    entity_type="user", entity_id=user.user_id,
                    ip_address=request.client.host if request.client else None))
    db.commit()
    return {"deleted": True,
            "message": "Your account has been deleted and your personal details erased."}


@router.get("/me/status")
def my_privacy_status(user: CurrentUser, db: DbSession):
    profile = db.scalar(select(Profile).where(Profile.user_id == user.user_id))
    if not profile:
        return {"listed": False, "has_profile": False}
    released = db.scalar(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "provider_contact_released",
            AuditLog.entity_id == profile.profile_id)) or 0
    return {
        "has_profile": True,
        "listed": bool(profile.is_listable),
        "opted_out": profile.screen_reason == OPT_OUT_REASON,
        # People reasonably want to know who has looked them up.
        "times_contact_released": released,
    }


# --- Anyone, by email ------------------------------------------------------

@router.post("/opt-out/request")
def request_opt_out(body: OptOutRequest, db: DbSession, request: Request):
    """Start an opt-out for an address, and email a confirmation link.

    Always answers the same way whether or not the address is in the directory,
    so this cannot be used to test who is listed.
    """
    addr = body.email.strip().lower()
    profiles = db.scalars(
        select(Profile).where(func.lower(func.btrim(Profile.email)) == addr)).all()
    issued = None
    for profile in profiles:
        token = secrets.token_urlsafe(24)
        issued = issued or token
        db.add(AuditLog(
            actor_user_id=None, action=ACTION_REQUEST, entity_type="profile",
            entity_id=profile.profile_id, meta={"token": token, "email": addr},
            ip_address=request.client.host if request.client else None))
    db.commit()

    if issued:
        base = settings.frontend_base_url.rstrip("/")
        link = f"{base}/api/privacy/opt-out/confirm?token={issued}"
        send_email(addr, "Confirm your removal request",
                   f"<p>Someone asked to remove this address from the HealthBoard "
                   f"provider directory.</p><p><a href='{link}'>Confirm removal</a></p>"
                   f"<p>If this wasn't you, ignore this email — nothing changes "
                   f"unless the link is used.</p>")
    response = {"status": "sent",
                "message": "If that address is in our directory, we've emailed a "
                           "confirmation link."}
    # Without a mail provider the link would be unreachable, so surface the
    # token in development the same way the auth flow does.
    if issued and not (settings.email_enabled and settings.sendgrid_api_key):
        response["dev_token"] = issued
    return response


@router.api_route("/opt-out/confirm", methods=["GET", "POST"])
def confirm_opt_out(request: Request, db: DbSession, token: str = ""):
    """Complete an opt-out. One token, one use."""
    token = (token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing token")
    rows = db.scalars(
        select(AuditLog).where(AuditLog.action == ACTION_REQUEST)).all()
    matched = [r for r in rows if (r.meta or {}).get("token") == token
               and not (r.meta or {}).get("used")]
    if not matched:
        raise HTTPException(status_code=404, detail="That link is invalid or already used")

    removed = 0
    for row in matched:
        profile = db.get(Profile, row.entity_id)
        if profile:
            _delist(db, profile, actor=None, request=request, via="email_confirmation")
            removed += 1
        row.meta = {**(row.meta or {}), "used": True, "used_at": utcnow().isoformat()}
    db.commit()
    return {"removed": removed,
            "message": "You have been removed from the directory and your contact "
                       "details erased."}
