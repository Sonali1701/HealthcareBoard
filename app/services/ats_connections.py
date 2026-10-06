"""Organization ATS credential storage and connection checks."""
from __future__ import annotations

import json
import ipaddress
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import utcnow
from ..config import settings as app_settings
from ..models import AtsOrganizationIntegration, Employer, EmployerMember, User
from .zoom_oauth import decrypt, encrypt


PROVIDERS = {"ceipal", "nexus"}
SECRET_FIELDS = {
    "ceipal": {"password", "api_key"},
    "nexus": {"password", "client_secret", "static_token", "token_basic"},
}
ALLOWED_FIELDS = {
    "ceipal": {"base_url", "email", "password", "api_key"},
    "nexus": {
        "base_url", "auth_method", "token_url", "token_payload_style",
        "client_id", "client_secret", "username", "password", "static_token",
        "token_basic", "org_code", "resume_doc_type_id", "default_profile",
    },
}


def normalize_provider(provider: str) -> str:
    value = str(provider or "").strip().casefold()
    if value not in PROVIDERS:
        raise HTTPException(404, "Supported ATS providers are CEIPAL and Nexus.")
    return value


def _safe_url(value: str, *, field: str) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username:
        raise HTTPException(422, f"Enter a valid {field} URL.")
    if app_settings.is_production and parsed.scheme != "https":
        raise HTTPException(422, f"The {field} URL must use HTTPS.")
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        address = None
    if address and (address.is_private or address.is_loopback or address.is_link_local):
        raise HTTPException(422, f"The {field} URL cannot use a private network address.")
    return url


def _validate(provider: str, supplied: dict, existing: dict | None = None) -> dict:
    provider = normalize_provider(provider)
    merged = dict(existing or {})
    for key in ALLOWED_FIELDS[provider]:
        if key not in supplied:
            continue
        value = supplied.get(key)
        if key == "default_profile":
            if isinstance(value, str):
                try:
                    value = json.loads(value or "{}")
                except ValueError as exc:
                    raise HTTPException(422, "Nexus default profile must be valid JSON.") from exc
            if not isinstance(value, dict):
                raise HTTPException(422, "Nexus default profile must be a JSON object.")
            merged[key] = value
        elif str(value or "").strip() or key not in SECRET_FIELDS[provider]:
            merged[key] = str(value or "").strip()
    if provider == "ceipal":
        merged["base_url"] = _safe_url(
            merged.get("base_url") or "https://api.ceipal.com", field="CEIPAL API"
        )
        missing = [key for key in ("email", "password", "api_key") if not merged.get(key)]
    else:
        merged["base_url"] = _safe_url(merged.get("base_url"), field="Nexus API")
        merged["auth_method"] = str(merged.get("auth_method") or "password").casefold()
        merged["token_payload_style"] = str(
            merged.get("token_payload_style") or "form"
        ).casefold()
        if merged["auth_method"] not in {"password", "client_credentials", "static"}:
            raise HTTPException(422, "Choose password, client credentials, or static token authentication.")
        if merged["token_payload_style"] not in {"form", "json"}:
            raise HTTPException(422, "Nexus token payload style must be form or JSON.")
        if merged["auth_method"] != "static":
            merged["token_url"] = _safe_url(merged.get("token_url"), field="Nexus token")
        if merged["auth_method"] == "password":
            missing = [key for key in ("username", "password") if not merged.get(key)]
        elif merged["auth_method"] == "client_credentials":
            missing = [] if (
                (merged.get("client_id") and merged.get("client_secret"))
                or merged.get("token_basic")
            ) else ["client credentials"]
        else:
            missing = [] if merged.get("static_token") else ["static token"]
    if missing:
        raise HTTPException(422, f"Missing {provider.upper()} setting: {', '.join(missing)}.")
    return merged


def settings(integration: AtsOrganizationIntegration) -> dict:
    try:
        value = json.loads(decrypt(integration.settings_encrypted))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(503, f"The {integration.provider.upper()} connection could not be read. Reconnect it.") from exc
    if not isinstance(value, dict):
        raise HTTPException(503, f"The {integration.provider.upper()} connection is invalid. Reconnect it.")
    return value


def public_status(integration: AtsOrganizationIntegration | None, provider: str) -> dict:
    provider = normalize_provider(provider)
    saved = settings(integration) if integration else {}
    visible = {
        key: value for key, value in saved.items()
        if key not in SECRET_FIELDS[provider]
    }
    return {
        "provider": provider,
        "connected": bool(integration and integration.status == "connected"),
        "status": integration.status if integration else "not_connected",
        "settings": visible,
        "secret_fields": {
            key: bool(saved.get(key)) for key in sorted(SECRET_FIELDS[provider])
        },
        "last_tested_at": integration.last_tested_at if integration else None,
        "last_error": integration.last_error if integration else None,
    }


def save(db: Session, employer: Employer, actor: User, provider: str,
         supplied: dict) -> AtsOrganizationIntegration:
    provider = normalize_provider(provider)
    integration = db.scalar(select(AtsOrganizationIntegration).where(
        AtsOrganizationIntegration.employer_id == employer.employer_id,
        AtsOrganizationIntegration.provider == provider,
    ))
    current = settings(integration) if integration else {}
    validated = _validate(provider, supplied, current)
    encrypted = encrypt(json.dumps(validated, separators=(",", ":"), sort_keys=True))
    if integration:
        integration.settings_encrypted = encrypted
        integration.status = "connected"
        integration.connected_by_user_id = actor.user_id
        integration.last_error = None
    else:
        integration = AtsOrganizationIntegration(
            employer_id=employer.employer_id, provider=provider,
            settings_encrypted=encrypted, status="connected",
            connected_by_user_id=actor.user_id,
        )
        db.add(integration)
    db.commit()
    db.refresh(integration)
    return integration


def test_connection(db: Session, integration: AtsOrganizationIntegration) -> dict:
    values = settings(integration)
    provider = normalize_provider(integration.provider)
    try:
        with httpx.Client(timeout=20) as client:
            if provider == "ceipal":
                response = client.post(
                    f"{values['base_url'].rstrip('/')}/v1/createAuthtoken",
                    json={"email": values["email"], "password": values["password"],
                          "api_key": values["api_key"]},
                )
            elif values.get("auth_method") == "static":
                response = None
            else:
                headers = {}
                auth = None
                if values.get("client_id") and values.get("client_secret"):
                    auth = (values["client_id"], values["client_secret"])
                elif values.get("token_basic"):
                    basic = str(values["token_basic"])
                    headers["Authorization"] = basic if basic.lower().startswith("basic ") else f"Basic {basic}"
                payload = {"grant_type": values["auth_method"]}
                if values["auth_method"] == "password":
                    payload.update(username=values["username"], password=values["password"])
                if values.get("org_code"):
                    payload["organizationCode"] = values["org_code"]
                    headers["organizationCode"] = values["org_code"]
                kwargs = {"json": payload} if values.get("token_payload_style") == "json" else {"data": payload}
                response = client.post(values["token_url"], auth=auth, headers=headers, **kwargs)
        if response is not None:
            if response.is_error:
                raise RuntimeError(f"authentication returned HTTP {response.status_code}")
            try:
                body = response.json()
            except ValueError as exc:
                raise RuntimeError("authentication returned invalid JSON") from exc
            sources = [body, body.get("data") if isinstance(body, dict) else None]
            if not any(
                isinstance(source, dict)
                and any(source.get(key) for key in ("access_token", "acess_token", "token", "jwt"))
                for source in sources
            ):
                raise RuntimeError("authentication returned no access token")
        integration.status = "connected"
        integration.last_error = None
        integration.last_tested_at = utcnow()
        db.commit()
        return {"ok": True, "provider": provider}
    except (httpx.HTTPError, RuntimeError, KeyError) as exc:
        integration.status = "error"
        integration.last_error = f"Connection test failed: {exc}"
        integration.last_tested_at = utcnow()
        db.commit()
        raise HTTPException(502, integration.last_error) from exc


def for_user(db: Session, user_id: str, provider: str, *,
             connected_only: bool = True) -> tuple[Employer, AtsOrganizationIntegration] | None:
    provider = normalize_provider(provider)
    employer_ids = set(db.scalars(select(Employer.employer_id).where(
        Employer.owner_user_id == user_id,
    )).all())
    employer_ids.update(db.scalars(select(EmployerMember.employer_id).where(
        EmployerMember.user_id == user_id,
    )).all())
    if not employer_ids:
        return None
    query = (
        select(Employer, AtsOrganizationIntegration)
        .join(AtsOrganizationIntegration, AtsOrganizationIntegration.employer_id == Employer.employer_id)
        .where(
            Employer.employer_id.in_(employer_ids),
            AtsOrganizationIntegration.provider == provider,
        )
    )
    if connected_only:
        query = query.where(AtsOrganizationIntegration.status == "connected")
    rows = db.execute(query).all()
    if not rows:
        return None
    if len(rows) > 1:
        raise HTTPException(409, f"This user belongs to multiple organizations with {provider.upper()} connected.")
    return rows[0][0], rows[0][1]
