"""External integrations — trigger a Ceipal jobs sync from the app."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..deps import CurrentUser
from ..importers.ceipal_jobs import run as ceipal_run
from ..services.ceipal import CeipalError

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
