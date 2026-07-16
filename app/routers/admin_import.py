"""Authoritative bulk-import endpoint — the job board as the single gatekeeper.

A trusted uploader (e.g. data_upload.py in --server mode) POSTs each résumé file
here; the server parses it, rejects duplicates, hides junk-name profiles, stores
the file and inserts the profile — all via app.services.ingestion. Because the
logic lives here, no stale copy of a client script can bypass the rules.

Auth is a shared secret (X-Import-Token) rather than a short-lived JWT, so long
bulk runs don't expire mid-way. The endpoint is DISABLED unless
settings.import_api_token is set (see .env).
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile, status

from ..config import settings
from ..deps import DbSession
from ..services import ingestion

router = APIRouter(prefix="/api/admin", tags=["admin"])

_MAX_BYTES = 15 * 1024 * 1024  # 15 MB per résumé — plenty; guards against abuse
_ALLOWED_EXT = (".pdf", ".docx")


def _require_import_token(x_import_token: str = Header(default="")) -> None:
    configured = settings.import_api_token
    if not configured:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not x_import_token or not secrets.compare_digest(x_import_token, configured):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or missing X-Import-Token")


@router.post("/import", dependencies=[Depends(_require_import_token)])
async def import_resume(db: DbSession, file: UploadFile = File(...)) -> dict:
    """Ingest a single résumé. Returns {status: created|duplicate, ...}."""
    name = file.filename or "resume.pdf"
    if not name.lower().endswith(_ALLOWED_EXT):
        raise HTTPException(status_code=400, detail="Only .pdf and .docx are supported")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large")

    try:
        result = ingestion.ingest_resume_bytes(db, data, name)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise HTTPException(status_code=422, detail=f"Ingestion failed: {exc}")
    return result
