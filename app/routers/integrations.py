"""External integrations — trigger a Ceipal jobs sync from the app."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from ..config import settings
from ..deps import CurrentUser, DbSession
from ..importers.ceipal_jobs import run as ceipal_run
from ..models import AuditLog, Employer, User, ZoomOrganizationIntegration
from ..services.ceipal import CeipalError
from ..services import org_roles, zoom_oauth

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


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
