"""Zoom organization OAuth token lifecycle and member directory sync."""
from __future__ import annotations

import base64
import hashlib
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import utcnow
from ..models import (
    Employer, EmployerMember, MedhuntMessagingPermission, MedhuntSmsSender,
    User, ZoomOrganizationIntegration,
)


def configured() -> bool:
    return bool(settings.zoom_oauth_client_id and settings.zoom_oauth_client_secret
                and settings.zoom_oauth_redirect_uri)


def _fernet() -> Fernet:
    seed = settings.integration_encryption_key or settings.jwt_secret
    if not seed or seed == "dev-only-insecure-secret-change-me":
        if settings.is_production:
            raise HTTPException(503, "Set INTEGRATION_ENCRYPTION_KEY before connecting Zoom.")
    key = base64.urlsafe_b64encode(hashlib.sha256(seed.encode()).digest())
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise HTTPException(503, "The Zoom connection could not be decrypted. Reconnect Zoom.") from exc


def authorization_url(employer_id: str, user_id: str) -> str:
    if not configured():
        raise HTTPException(503, "Zoom OAuth is not configured.")
    state = URLSafeTimedSerializer(settings.jwt_secret, salt="zoom-organization-oauth").dumps({
        "employer_id": employer_id, "user_id": user_id,
    })
    query = {
        "response_type": "code", "client_id": settings.zoom_oauth_client_id,
        "redirect_uri": settings.zoom_oauth_redirect_uri, "state": state,
    }
    if settings.zoom_oauth_scopes:
        query["scope"] = settings.zoom_oauth_scopes
    return f"{settings.zoom_oauth_authorize_url}?{urlencode(query)}"


def state_payload(state: str) -> dict:
    try:
        return URLSafeTimedSerializer(settings.jwt_secret, salt="zoom-organization-oauth").loads(
            state, max_age=600,
        )
    except SignatureExpired as exc:
        raise HTTPException(400, "The Zoom connection request expired. Start again from Halo.") from exc
    except BadSignature as exc:
        raise HTTPException(400, "Invalid Zoom connection request.") from exc


def exchange_code(code: str) -> dict:
    response = httpx.post(
        settings.zoom_oauth_token_url,
        data={"grant_type": "authorization_code", "code": code,
              "redirect_uri": settings.zoom_oauth_redirect_uri},
        auth=(settings.zoom_oauth_client_id, settings.zoom_oauth_client_secret), timeout=20,
    )
    if response.is_error:
        raise HTTPException(502, "Zoom rejected the authorization code.")
    return response.json()


def _refresh(db: Session, integration: ZoomOrganizationIntegration) -> str:
    response = httpx.post(
        settings.zoom_oauth_token_url,
        data={"grant_type": "refresh_token",
              "refresh_token": decrypt(integration.refresh_token_encrypted)},
        auth=(settings.zoom_oauth_client_id, settings.zoom_oauth_client_secret), timeout=20,
    )
    if response.is_error:
        integration.status = "error"
        integration.last_error = "Zoom token refresh failed; reconnect Zoom."
        db.commit()
        raise HTTPException(503, integration.last_error)
    payload = response.json()
    integration.access_token_encrypted = encrypt(str(payload["access_token"]))
    if payload.get("refresh_token"):
        integration.refresh_token_encrypted = encrypt(str(payload["refresh_token"]))
    integration.access_token_expires_at = utcnow() + timedelta(seconds=max(60, int(payload.get("expires_in") or 3600)))
    integration.scopes = str(payload.get("scope") or integration.scopes or "")
    integration.status = "connected"
    integration.last_error = None
    db.commit()
    return str(payload["access_token"])


def access_token(db: Session, integration: ZoomOrganizationIntegration) -> str:
    if integration.status not in {"connected", "error"}:
        raise HTTPException(409, "Reconnect this organization's Zoom account.")
    if integration.access_token_expires_at <= utcnow() + timedelta(seconds=90):
        return _refresh(db, integration)
    return decrypt(integration.access_token_encrypted)


def save_connection(db: Session, employer: Employer, actor: User, payload: dict) -> ZoomOrganizationIntegration:
    token = str(payload.get("access_token") or "")
    refresh = str(payload.get("refresh_token") or "")
    if not token or not refresh:
        raise HTTPException(502, "Zoom did not return a reusable organization authorization.")
    account_id = str(payload.get("account_id") or payload.get("owner_id") or "")
    integration = db.scalar(select(ZoomOrganizationIntegration).where(
        ZoomOrganizationIntegration.employer_id == employer.employer_id,
    ))
    values = dict(
        zoom_account_id=account_id or None, status="connected",
        access_token_encrypted=encrypt(token), refresh_token_encrypted=encrypt(refresh),
        access_token_expires_at=utcnow() + timedelta(seconds=max(60, int(payload.get("expires_in") or 3600))),
        scopes=str(payload.get("scope") or ""), connected_by_user_id=actor.user_id,
        last_error=None,
    )
    if integration:
        for key, value in values.items():
            setattr(integration, key, value)
    else:
        integration = ZoomOrganizationIntegration(employer_id=employer.employer_id, **values)
        db.add(integration)
    db.commit()
    db.refresh(integration)
    return integration


def _zoom_get(token: str, path: str, params: dict | None = None) -> dict:
    response = httpx.get(
        f"{settings.zoom_api_base_url.rstrip('/')}{path}",
        params=params, headers={"Authorization": f"Bearer {token}"}, timeout=20,
    )
    if response.is_error:
        raise HTTPException(502, "Zoom Phone user sync failed. Check the app scopes and Zoom Phone licenses.")
    return response.json()


def sync_members(db: Session, employer: Employer, integration: ZoomOrganizationIntegration,
                 actor: User) -> dict:
    token = access_token(db, integration)
    zoom_users: list[dict] = []
    next_page_token = ""
    for _ in range(20):
        payload = _zoom_get(token, "/phone/users", {
            "page_size": 100, **({"next_page_token": next_page_token} if next_page_token else {}),
        })
        zoom_users.extend(payload.get("users") or [])
        next_page_token = str(payload.get("next_page_token") or "")
        if not next_page_token:
            break
    org_ids = {employer.owner_user_id, *db.scalars(select(EmployerMember.user_id).where(
        EmployerMember.employer_id == employer.employer_id,
    )).all()}
    halo_users = {item.email.casefold(): item for item in db.scalars(select(User).where(
        User.user_id.in_(org_ids), User.deleted_at.is_(None),
    )).all()}
    matched = configured_numbers = 0
    missing_number: list[str] = []
    for zoom_user in zoom_users:
        email = str(zoom_user.get("email") or "").strip().casefold()
        halo_user = halo_users.get(email)
        if not halo_user:
            continue
        matched += 1
        numbers = zoom_user.get("phone_numbers") or []
        number = next((str(item.get("number") or item.get("phone_number") or "").strip()
                       for item in numbers if isinstance(item, dict)
                       and (item.get("number") or item.get("phone_number"))), "")
        if not number:
            missing_number.append(halo_user.email)
            continue
        digits = "".join(character for character in number if character.isdigit())
        if len(digits) == 10:
            digits = "1" + digits
        if not 8 <= len(digits) <= 15:
            missing_number.append(halo_user.email)
            continue
        normalized = "+" + digits
        sender = db.scalar(select(MedhuntSmsSender).where(
            MedhuntSmsSender.employer_id == employer.employer_id,
            MedhuntSmsSender.user_id == halo_user.user_id,
        ))
        if sender:
            sender.sender_number = normalized
            sender.zoom_user_id = str(zoom_user.get("id") or email)
            sender.updated_by_user_id = actor.user_id
        else:
            db.add(MedhuntSmsSender(
                employer_id=employer.employer_id, user_id=halo_user.user_id,
                sender_number=normalized, zoom_user_id=str(zoom_user.get("id") or email),
                updated_by_user_id=actor.user_id,
            ))
        permission = db.scalar(select(MedhuntMessagingPermission).where(
            MedhuntMessagingPermission.employer_id == employer.employer_id,
            MedhuntMessagingPermission.user_id == halo_user.user_id,
        ))
        if not permission:
            db.add(MedhuntMessagingPermission(
                employer_id=employer.employer_id, user_id=halo_user.user_id,
                status="disabled", reason="Synced from Zoom; awaiting messaging approval",
                updated_by_user_id=actor.user_id,
            ))
        configured_numbers += 1
    integration.last_synced_at = utcnow()
    integration.last_error = None
    db.commit()
    return {"zoom_users": len(zoom_users), "matched_members": matched,
            "configured_senders": configured_numbers, "missing_number": missing_number}
