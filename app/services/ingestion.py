"""Authoritative résumé ingestion — the single gatekeeper for the job board.

Every path that adds a provider profile (the admin import endpoint, the folder
importer) goes through `ingest_resume_bytes`, so the rules live in exactly ONE
place and cannot drift between a standalone script and the server:

  1. parse the résumé (heuristics; optional LLM name refinement)
  2. REJECT duplicates — same person already in the DB (NPI -> email -> name+phone)
  3. FLAG irrelevant/junk — a placeholder/role/document "name" is stored hidden
     (is_listable=False) so it never pollutes the directory
  4. store the file (content-addressed) and insert the Profile + child rows

Returns a small result dict: {status, reason, profile_id, name, listable}.
`status` is one of: created | duplicate | error.
"""
from __future__ import annotations

import hashlib
import io
import re
from datetime import date
from pathlib import Path

from sqlalchemy import func, select

from ..config import settings
from ..importers.parsing import (extract_text_from_bytes, is_real_name,
                                 parse_resume)
from ..models import Certification, License, Profile, ProfileSkill
from ..models.enums import LicenseStatus, ProfileSource

_PDF = "application/pdf"
_DOCX = ("application/vnd.openxmlformats-officedocument."
         "wordprocessingml.document")


# --- duplicate detection (mirrors app/dedup_profiles.py keys) --------------

def _phone10(v: str | None) -> str | None:
    digits = re.sub(r"\D", "", v or "")
    return digits[-10:] if len(digits) >= 10 else None


def find_duplicate(db, fields: dict) -> tuple[str | None, str | None]:
    """Return (existing_profile_id, matched_key) for the same person, else (None, None)."""
    npi = (str(fields.get("npi_number") or "").strip()) or None
    if npi:
        row = db.execute(
            select(Profile.profile_id).where(Profile.npi_number == npi).limit(1)
        ).first()
        if row:
            return row[0], "npi"

    email = (str(fields.get("email") or "").strip().lower()) or None
    if email and "@" in email:
        row = db.execute(
            select(Profile.profile_id).where(func.lower(Profile.email) == email).limit(1)
        ).first()
        if row:
            return row[0], "email"

    ph = _phone10(fields.get("phone"))
    last = (str(fields.get("last_name") or "").strip().lower()) or None
    if ph and last:
        try:
            row = db.execute(
                select(Profile.profile_id).where(
                    func.lower(Profile.last_name) == last,
                    func.right(func.regexp_replace(
                        func.coalesce(Profile.phone, ""), "[^0-9]", "", "g"), 10) == ph,
                ).limit(1)
            ).first()
            if row:
                return row[0], "phone"
        except Exception:  # noqa: BLE001 — regexp_replace is Postgres-only; skip elsewhere
            pass
    return None, None


# --- optional LLM name refinement (no-op if disabled / unavailable) ---------

def _llm_refine(text: str, fields: dict) -> None:
    """If the heuristic name looks junky and an LLM is configured, try to recover
    the real name. Silent fallback — never raises."""
    if not (settings.llm_enabled and settings.llm_api_key and settings.llm_model):
        return
    if is_real_name(fields.get("first_name"), fields.get("last_name")):
        return
    try:
        from ..clean_names_llm import _llm, _title
        raw = _llm(text)
        if not raw:
            return
        first, last = _title(raw.get("first_name")), _title(raw.get("last_name"))
        if first and last and is_real_name(first, last):
            fields["first_name"], fields["last_name"] = first[:100], last[:100]
            for k, cap in (("specialty", 100), ("city", 120)):
                if raw.get(k):
                    fields[k] = (_title(raw[k]) or fields.get(k))
    except Exception:  # noqa: BLE001
        return


def _completion(fields: dict) -> int:
    score = 20  # has a résumé on file
    if fields.get("specialty"):
        score += 15
    if fields.get("profession_type"):
        score += 10
    if fields.get("city"):
        score += 5
    if fields.get("years_experience"):
        score += 5
    if fields.get("state_code"):
        score += 10
    if fields.get("certifications"):
        score += 10
    if fields.get("licenses"):
        score += 10
    if fields.get("bio"):
        score += 5
    if fields.get("email"):
        score += 10
    return min(score, 100)


def _content_key(data: bytes, filename: str) -> str:
    digest = hashlib.sha256(data).hexdigest()
    ext = (Path(filename).suffix or ".pdf").lower()
    return f"resumes/import/{digest[:2]}/{digest}{ext}"


def parse_resume_bytes(data: bytes, filename: str) -> dict:
    """Parse a résumé file into candidate fields (no DB, no storage). Used by the
    ingest endpoint to de-dup/merge before deciding to create."""
    text = extract_text_from_bytes(data, filename)
    fields = parse_resume(text, Path(filename))
    _llm_refine(text, fields)   # silent no-op if the LLM is unavailable
    return fields


def store_resume_file(data: bytes, filename: str) -> str:
    """Store a résumé (content-addressed) and return its URL."""
    from . import storage
    key = _content_key(data, filename)
    ctype = _PDF if key.endswith(".pdf") else _DOCX
    return storage.upload(io.BytesIO(data), key, ctype)


def ingest_resume_bytes(db, data: bytes, filename: str, *,
                        source: ProfileSource = ProfileSource.resume_parse,
                        do_commit: bool = True) -> dict:
    """Parse, de-dup, junk-flag, store and insert one résumé. See module docstring."""
    text = extract_text_from_bytes(data, filename)
    fields = parse_resume(text, Path(filename))
    _llm_refine(text, fields)

    # 1) Reject exact-same-person duplicates.
    dup_id, dup_key = find_duplicate(db, fields)
    if dup_id:
        return {"status": "duplicate", "reason": dup_key, "profile_id": dup_id,
                "name": f"{fields.get('first_name','')} {fields.get('last_name','')}".strip(),
                "listable": None}

    # 2) Flag junk/irrelevant names -> stored but hidden from the directory.
    listable = is_real_name(fields.get("first_name"), fields.get("last_name"))

    # 3) Store the file (content-addressed so identical bytes reuse the key).
    from . import storage  # local import keeps parsing importable without boto3
    key = _content_key(data, filename)
    ctype = _PDF if key.endswith(".pdf") else _DOCX
    resume_url = storage.upload(io.BytesIO(data), key, ctype)

    # 4) Insert the profile + child rows.
    profile = Profile(
        user_id=None,
        first_name=fields["first_name"],
        last_name=fields["last_name"],
        headline=fields.get("headline"),
        bio=fields.get("bio"),
        phone=fields.get("phone"),
        email=fields.get("email"),
        specialty=fields.get("specialty"),
        profession_type=fields.get("profession_type"),
        provider_category=fields.get("provider_category"),
        american_board=fields.get("american_board"),
        is_listable=listable,
        years_experience=fields.get("years_experience") or 0,
        city=fields.get("city"),
        state_code=fields.get("state_code"),
        zip_code=fields.get("zip_code"),
        npi_number=(fields.get("npi_number") or None),
        resume_url=resume_url,
        open_to_work=True,
        source=source,
        completion_score=_completion(fields),
    )
    profile.rebuild_search_text()
    db.add(profile)
    db.flush()

    for cert in fields.get("certifications", []):
        db.add(Certification(profile_id=profile.profile_id, cert_name=cert[:100]))
    for lic in fields.get("licenses", []):
        expiry = date(lic["expiry_year"], 12, 31) if lic.get("expiry_year") else None
        db.add(License(
            profile_id=profile.profile_id,
            license_type=lic.get("license_type") or "MD",
            license_number="(from resume)",
            state_code=lic.get("state_code"),
            status=LicenseStatus.active,
            expiry_date=expiry,
            verification_source="resume",
        ))
    if fields.get("specialty"):
        db.add(ProfileSkill(profile_id=profile.profile_id,
                            name=fields["specialty"][:100],
                            years=fields.get("years_experience") or None))
    if do_commit:
        db.commit()

    return {"status": "created", "reason": None, "profile_id": profile.profile_id,
            "name": f"{profile.first_name} {profile.last_name}".strip(),
            "listable": listable}
