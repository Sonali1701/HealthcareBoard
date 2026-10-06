"""External integrations — trigger a Ceipal jobs sync from the app."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..config import settings
from ..deps import CurrentUser, DbSession
from ..importers.ceipal_jobs import run as ceipal_run
from ..models import (
    AtsOrganizationIntegration, AuditLog, Employer, EmployerMember, User,
    ZoomOrganizationIntegration,
)
from ..services.ceipal import CeipalError
from ..services import ats_connections, org_roles, zoom_oauth

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


class AtsConnectionPatch(BaseModel):
    settings: dict = Field(default_factory=dict)


class AtsMemberRoutePatch(BaseModel):
    provider: str = Field(pattern=r"^(ceipal|nexus)$")


@router.post("/ceipal/sync")
def ceipal_sync(user: CurrentUser):
    """Pull the latest jobs from Ceipal into the board (recruiter/admin only)."""
    if user.role.value not in ("recruiter", "employer", "admin"):
        raise HTTPException(status_code=403, detail="Recruiter access required")
    try:
        summary = ceipal_run(inspect=False)
        return {"status": "ok", **(summary or {})}
    except CeipalError as e:
        raise HTTPException(status_code=502, detail=str(e))


def _zoom_admin(db: DbSession, employer_id: str, user: CurrentUser) -> Employer:
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(404, "Organization not found")
    if not org_roles.can(org_roles.role_of(db, employer, user), "settings"):
        raise HTTPException(403, "Only an organization owner or admin can manage Zoom.")
    return employer


def _integration_admin(db: DbSession, employer_id: str, user: CurrentUser) -> Employer:
    employer = db.get(Employer, employer_id)
    if not employer:
        raise HTTPException(404, "Organization not found")
    if not org_roles.can(org_roles.role_of(db, employer, user), "settings"):
        raise HTTPException(403, "Only an organization owner or admin can manage integrations.")
    return employer


@router.get("/ats/status")
def ats_status(employer_id: str, user: CurrentUser, db: DbSession):
    employer = _integration_admin(db, employer_id, user)
    rows = db.scalars(select(AtsOrganizationIntegration).where(
        AtsOrganizationIntegration.employer_id == employer.employer_id,
    )).all()
    by_provider = {row.provider: row for row in rows}
    return {
        "items": [
            ats_connections.public_status(by_provider.get(provider), provider)
            for provider in sorted(ats_connections.PROVIDERS)
        ]
    }


@router.put("/ats/{provider}")
def save_ats_connection(provider: str, body: AtsConnectionPatch,
                        employer_id: str, user: CurrentUser, db: DbSession):
    employer = _integration_admin(db, employer_id, user)
    integration = ats_connections.save(db, employer, user, provider, body.settings)
    db.add(AuditLog(
        actor_user_id=user.user_id, action="ats_organization_connected",
        entity_type="employer", entity_id=employer.employer_id,
        meta={"provider": integration.provider},
    ))
    db.commit()
    return ats_connections.public_status(integration, integration.provider)


@router.post("/ats/{provider}/test")
def test_ats_connection(provider: str, employer_id: str,
                        user: CurrentUser, db: DbSession):
    employer = _integration_admin(db, employer_id, user)
    normalized = ats_connections.normalize_provider(provider)
    integration = db.scalar(select(AtsOrganizationIntegration).where(
        AtsOrganizationIntegration.employer_id == employer.employer_id,
        AtsOrganizationIntegration.provider == normalized,
    ))
    if not integration:
        raise HTTPException(409, f"Connect {normalized.upper()} first.")
    return ats_connections.test_connection(db, integration)


@router.delete("/ats/{provider}")
def disconnect_ats(provider: str, employer_id: str,
                   user: CurrentUser, db: DbSession):
    employer = _integration_admin(db, employer_id, user)
    normalized = ats_connections.normalize_provider(provider)
    integration = db.scalar(select(AtsOrganizationIntegration).where(
        AtsOrganizationIntegration.employer_id == employer.employer_id,
        AtsOrganizationIntegration.provider == normalized,
    ))
    if integration:
        integration.status = "disconnected"
        integration.settings_encrypted = ats_connections.encrypt("{}")
        integration.last_error = None
        db.add(AuditLog(
            actor_user_id=user.user_id, action="ats_organization_disconnected",
            entity_type="employer", entity_id=employer.employer_id,
            meta={"provider": normalized},
        ))
        db.commit()
    return {"disconnected": True, "provider": normalized}


@router.patch("/ats/members/{target_user_id}")
def set_member_ats_route(target_user_id: str, body: AtsMemberRoutePatch,
                         employer_id: str, user: CurrentUser, db: DbSession):
    employer = _integration_admin(db, employer_id, user)
    if target_user_id != employer.owner_user_id and not db.scalar(
        select(EmployerMember).where(
            EmployerMember.employer_id == employer.employer_id,
            EmployerMember.user_id == target_user_id,
        )
    ):
        raise HTTPException(404, "That user is not a member of this organization.")
    provider = ats_connections.normalize_provider(body.provider)
    connected = db.scalar(select(AtsOrganizationIntegration).where(
        AtsOrganizationIntegration.employer_id == employer.employer_id,
        AtsOrganizationIntegration.provider == provider,
        AtsOrganizationIntegration.status == "connected",
    ))
    if not connected:
        raise HTTPException(409, f"Connect the organization's {provider.upper()} account first.")
    target = db.get(User, target_user_id)
    if not target or target.deleted_at is not None:
        raise HTTPException(404, "Organization member not found.")
    target.medhunt_ceipal_enabled = provider == "ceipal"
    target.medhunt_nexus_enabled = provider == "nexus"
    db.add(AuditLog(
        actor_user_id=user.user_id, action="ats_member_route_changed",
        entity_type="user", entity_id=target.user_id,
        meta={"provider": provider, "employer_id": employer.employer_id},
    ))
    db.commit()
    return {"user_id": target.user_id, "provider": provider}


@router.get("/zoom/status")
def zoom_status(employer_id: str, user: CurrentUser, db: DbSession):
    employer = _zoom_admin(db, employer_id, user)
    integration = db.scalar(select(ZoomOrganizationIntegration).where(
        ZoomOrganizationIntegration.employer_id == employer.employer_id,
    ))
    return {
        "configured": zoom_oauth.configured(),
        "connected": bool(integration and integration.status == "connected"),
        "status": integration.status if integration else "not_connected",
        "zoom_account_id": integration.zoom_account_id if integration else None,
        "last_synced_at": integration.last_synced_at if integration else None,
        "last_error": integration.last_error if integration else None,
    }


@router.get("/zoom/connect")
def zoom_connect(employer_id: str, user: CurrentUser, db: DbSession):
    employer = _zoom_admin(db, employer_id, user)
    return {"authorization_url": zoom_oauth.authorization_url(employer.employer_id, user.user_id)}


@router.get("/zoom/callback", include_in_schema=False)
def zoom_callback(db: DbSession, code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(f"{settings.frontend_base_url.rstrip('/')}/?zoom=denied#organization")
    if not code or not state:
        raise HTTPException(400, "Zoom did not return an authorization code.")
    context = zoom_oauth.state_payload(state)
    employer = db.get(Employer, str(context.get("employer_id") or ""))
    actor = db.get(User, str(context.get("user_id") or ""))
    if not employer or not actor or not org_roles.can(org_roles.role_of(db, employer, actor), "settings"):
        raise HTTPException(403, "The organization administrator is no longer authorized.")
    integration = zoom_oauth.save_connection(db, employer, actor, zoom_oauth.exchange_code(code))
    try:
        zoom_oauth.sync_members(db, employer, integration, actor)
    except HTTPException as exc:
        integration.last_error = str(exc.detail)
        db.commit()
    db.add(AuditLog(
        actor_user_id=actor.user_id, action="zoom_organization_connected",
        entity_type="employer", entity_id=employer.employer_id,
        meta={"zoom_account_id": integration.zoom_account_id},
    ))
    db.commit()
    return RedirectResponse(f"{settings.frontend_base_url.rstrip('/')}/?zoom=connected#organization")


@router.post("/zoom/sync")
def zoom_sync(employer_id: str, user: CurrentUser, db: DbSession):
    employer = _zoom_admin(db, employer_id, user)
    integration = db.scalar(select(ZoomOrganizationIntegration).where(
        ZoomOrganizationIntegration.employer_id == employer.employer_id,
    ))
    if not integration:
        raise HTTPException(409, "Connect the organization's Zoom account first.")
    return zoom_oauth.sync_members(db, employer, integration, user)


@router.delete("/zoom")
def zoom_disconnect(employer_id: str, user: CurrentUser, db: DbSession):
    employer = _zoom_admin(db, employer_id, user)
    integration = db.scalar(select(ZoomOrganizationIntegration).where(
        ZoomOrganizationIntegration.employer_id == employer.employer_id,
    ))
    if integration:
        db.delete(integration)
        db.add(AuditLog(
            actor_user_id=user.user_id, action="zoom_organization_disconnected",
            entity_type="employer", entity_id=employer.employer_id, meta={},
        ))
        db.commit()
    return {"disconnected": True}
