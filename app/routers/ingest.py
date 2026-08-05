"""Central candidate ingest — the single write path into the candidate database.

The browser extension (and any client-enriched capture) POSTs a candidate here.
The server de-duplicates in real time, MERGES new fields into an existing profile
or CREATES a new one, records recruiter ownership, and reports whether the
candidate already existed and who owns them.

This is the linchpin for two requirements at once:
  • "before creating a profile, check for duplicates and merge"
  • the extension saving directly into the central ATS (not a side database).
"""
from __future__ import annotations

import hashlib
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from ..database import utcnow
from ..deps import CurrentUser, DbSession, IngestUser
from ..importers.parsing import classify_provider, is_real_name
from ..models import Profile
from ..models.enums import ProfileSource
from ..services import ingestion
from ..services.ingestion import find_duplicate

router = APIRouter(prefix="/api/ingest", tags=["ingest"])

_MAX_BYTES = 15 * 1024 * 1024
_ALLOWED_EXT = (".pdf", ".docx")


class CandidateIn(BaseModel):
    first_name: str
    last_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    city: Optional[str] = None
    state_code: Optional[str] = None
    specialty: Optional[str] = None
    profession_type: Optional[str] = None
    headline: Optional[str] = None
    years_experience: Optional[int] = None
    npi_number: Optional[str] = None
    resume_url: Optional[str] = None
    source: Optional[str] = None        # e.g. "indeed_extension", "vivian_extension"
    external_id: Optional[str] = None   # the source's own candidate id (reference only)


def _require_recruiter(user: CurrentUser) -> None:
    if user.role.value not in {"recruiter", "admin"}:
        raise HTTPException(status_code=403,
                            detail="Candidate capture is available to recruiter accounts.")


# Scalar fields we fill into an existing profile when it's missing them (merge is
# fill-empty — a capture never overwrites data already on file).
_MERGE_SCALARS = ("email", "phone", "city", "state_code", "specialty",
                  "profession_type", "headline", "npi_number", "resume_url")


def _clean_state(v) -> Optional[str]:
    s = str(v or "").strip().upper()
    return s if len(s) == 2 and s.isalpha() else None


def _merge_into(profile: Profile, fields: dict, user: CurrentUser,
                source: Optional[str], now) -> list:
    """Fill empty fields on an existing profile (never overwrite), adopt it if
    it's unowned, and keep its category/search text current. Returns filled fields."""
    filled = []
    for f in _MERGE_SCALARS:
        val = fields.get(f)
        val = val.strip() if isinstance(val, str) else val
        if val and not str(getattr(profile, f, None) or "").strip():
            setattr(profile, f, val)
            filled.append(f)
    if fields.get("years_experience") and not profile.years_experience:
        profile.years_experience = int(fields["years_experience"])
        filled.append("years_experience")
    if not profile.captured_by_user_id:        # unowned → first capturer adopts it
        profile.captured_by_user_id = user.user_id
        profile.captured_by_email = user.email
        profile.capture_source = (source or "extension")[:60]
        profile.captured_at = now
    if not profile.provider_category:
        profile.provider_category = classify_provider(
            profile.profession_type, profile.specialty, profile.headline)
    if filled:
        profile.rebuild_search_text()
    profile.updated_at = now
    return filled


def _merged_result(profile: Profile, user: CurrentUser, matched, filled) -> dict:
    return {
        "action": "merged", "existed": True, "profile_id": profile.profile_id,
        "matched_on": matched, "filled": filled,
        "owner_email": profile.captured_by_email,
        "owned_by_you": bool(profile.captured_by_user_id
                             and profile.captured_by_user_id == user.user_id),
        "captured_source": profile.capture_source,
        "last_contact_activity_by": profile.contact_updated_by_email,
        "message": (f"Already in the system — merged {len(filled)} new field(s)."
                    if filled else "Already in the system — nothing new to add."),
    }


@router.post("/candidate")
def ingest_candidate(body: CandidateIn, user: IngestUser, db: DbSession):
    """Capture a candidate into the central DB: dedupe → merge or create."""
    _require_recruiter(user)
    fields = body.model_dump()
    first = (fields.get("first_name") or "").strip()
    last = (fields.get("last_name") or "").strip()
    if not (first and last):
        raise HTTPException(status_code=422, detail="first_name and last_name are required.")
    fields["state_code"] = _clean_state(fields.get("state_code"))
    now = utcnow()

    existing_id, matched = find_duplicate(db, fields)

    if existing_id:
        profile = db.get(Profile, existing_id)
        filled = _merge_into(profile, fields, user, fields.get("source"), now)
        db.commit()
        return _merged_result(profile, user, matched, filled)

    # No match → create a fresh profile, owned by the capturing recruiter.
    profile = Profile(
        first_name=first[:100], last_name=last[:100],
        email=(fields.get("email") or None), phone=(fields.get("phone") or None),
        city=fields.get("city"), state_code=fields.get("state_code"),
        specialty=fields.get("specialty"), profession_type=fields.get("profession_type"),
        headline=fields.get("headline"), npi_number=(fields.get("npi_number") or None),
        resume_url=fields.get("resume_url"),
        years_experience=int(fields["years_experience"]) if fields.get("years_experience") else 0,
        is_listable=is_real_name(first, last),
        source=ProfileSource.resume_parse,
        capture_source=(fields.get("source") or "extension")[:60],
        captured_by_user_id=user.user_id, captured_by_email=user.email, captured_at=now,
    )
    profile.provider_category = classify_provider(
        profile.profession_type, profile.specialty, profile.headline)
    profile.rebuild_search_text()
    db.add(profile)
    db.commit()
    return {
        "action": "created",
        "existed": False,
        "profile_id": profile.profile_id,
        "owner_email": user.email,
        "owned_by_you": True,
        "message": "New candidate saved to the central database.",
    }


@router.post("/resume")
async def ingest_resume(user: IngestUser, db: DbSession,
                        file: UploadFile = File(...),
                        source: str = Form("extension")):
    """Capture an actual résumé FILE: parse it, then run it through the same
    dedupe → merge-or-create → ownership path (so the extension can push the real
    document and get a fully-parsed, de-duplicated, owned profile)."""
    _require_recruiter(user)
    name = file.filename or "resume.pdf"
    if not name.lower().endswith(_ALLOWED_EXT):
        raise HTTPException(status_code=400, detail="Only .pdf and .docx are supported.")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(data) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 15 MB).")

    try:
        fields = ingestion.parse_resume_bytes(data, name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Could not read résumé: {exc}")
    if not (fields.get("first_name") and fields.get("last_name")):
        raise HTTPException(status_code=422, detail="Couldn't find a candidate name in the résumé.")
    now = utcnow()

    existing_id, matched = find_duplicate(db, fields)
    if not existing_id:
        # Safety net for résumés with no email/phone/NPI: an identical file
        # (content-addressed résumé URL) is the same person.
        digest = hashlib.sha256(data).hexdigest()
        row = db.execute(
            select(Profile.profile_id).where(Profile.resume_url.like(f"%{digest}%")).limit(1)
        ).first()
        if row:
            existing_id, matched = row[0], "resume_file"

    if existing_id:
        profile = db.get(Profile, existing_id)
        filled = _merge_into(profile, fields, user, source, now)
        if not profile.resume_url:                      # attach the file if missing
            profile.resume_url = ingestion.store_resume_file(data, name)
            if "resume" not in filled:
                filled.append("resume")
        db.commit()
        return _merged_result(profile, user, matched, filled)

    # New candidate: reuse the authoritative create path (parse + store + child
    # rows), then stamp capture ownership onto the created profile.
    result = ingestion.ingest_resume_bytes(db, data, name)
    pid = result.get("profile_id")
    profile = db.get(Profile, pid) if pid else None
    if profile is not None:
        profile.capture_source = (source or "extension")[:60]
        profile.captured_by_user_id = user.user_id
        profile.captured_by_email = user.email
        profile.captured_at = now
        db.commit()
    return {
        "action": result.get("status", "created"),
        "existed": result.get("status") == "duplicate",
        "profile_id": pid,
        "name": result.get("name"),
        "listable": result.get("listable"),
        "owner_email": user.email,
        "owned_by_you": True,
        "message": "Résumé captured and saved to the central database.",
    }
