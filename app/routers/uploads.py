"""File upload endpoints for resumes and profile photos."""
from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile
from sqlalchemy import select

from ..deps import CurrentUser, DbSession
from ..importers.contact import backfill_missing_contact, extract_resume_fields
from ..models import Profile
from ..services import storage
from .profiles import _compute_completion

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

MAX_BYTES = 10 * 1024 * 1024  # 10 MB
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}
RESUME_TYPES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _fill_missing_resume_fields(profile: Profile, fields: dict) -> dict[str, str]:
    changed: dict[str, str] = {}
    for field in (
        "headline",
        "specialty",
        "profession_type",
        "provider_category",
        "american_board",
        "city",
        "state_code",
    ):
        value = fields.get(field)
        if value and not getattr(profile, field, None):
            setattr(profile, field, value)
            changed[field] = str(value)
    years = int(fields.get("years_experience") or 0)
    if years and not profile.years_experience:
        profile.years_experience = years
        changed["years_experience"] = str(years)
    return changed


def _profile(db: DbSession, user: CurrentUser) -> Profile:
    profile = db.scalar(select(Profile).where(Profile.user_id == user.user_id))
    if not profile:
        raise HTTPException(status_code=400, detail="Create a profile first")
    return profile


def _read_validated(file: UploadFile, allowed: set[str]) -> bytes:
    if file.content_type not in allowed:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported type {file.content_type}. Allowed: {sorted(allowed)}",
        )
    data = file.file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 10 MB limit")
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    return data


@router.post("/profile-photo")
def upload_profile_photo(user: CurrentUser, db: DbSession, file: UploadFile = File(...)):
    import io

    data = _read_validated(file, PHOTO_TYPES)
    profile = _profile(db, user)
    key = storage.build_key(f"photos/{profile.profile_id}", file.filename or "photo")
    url = storage.upload(io.BytesIO(data), key, file.content_type)
    profile.profile_photo_url = url
    db.commit()
    return {"profile_photo_url": url}


@router.post("/resume")
def upload_resume(user: CurrentUser, db: DbSession, file: UploadFile = File(...)):
    import io

    data = _read_validated(file, RESUME_TYPES)
    profile = _profile(db, user)
    key = storage.build_key(f"resumes/{profile.profile_id}", file.filename or "resume")
    url = storage.upload(io.BytesIO(data), key, file.content_type)
    profile.resume_url = url
    changed = backfill_missing_contact(profile, data, file.filename or "resume")
    try:
        changed.update(_fill_missing_resume_fields(
            profile, extract_resume_fields(data, file.filename or "resume")
        ))
    except Exception:
        pass
    profile.rebuild_search_text()
    profile.completion_score = _compute_completion(profile)
    db.commit()
    return {"resume_url": url, "contact_updated": changed}
