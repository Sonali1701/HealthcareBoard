"""Contact extraction helpers for uploaded resumes."""
from __future__ import annotations

from pathlib import Path

from .parsing import extract_text_from_bytes, parse_resume


def _extract_text(data: bytes, filename: str) -> str:
    return extract_text_from_bytes(data, filename)


def extract_resume_fields(data: bytes, filename: str) -> dict:
    return parse_resume(_extract_text(data, filename), Path(filename or "resume"))


def extract_contact(data: bytes, filename: str) -> dict[str, str | None]:
    fields = extract_resume_fields(data, filename)
    return {"email": fields.get("email"), "phone": fields.get("phone")}


def backfill_missing_contact(profile, data: bytes, filename: str) -> dict[str, str]:
    """Fill blank profile email/phone from a resume, without overwriting edits."""
    try:
        contact = extract_contact(data, filename)
    except Exception:
        return {}

    changed: dict[str, str] = {}
    for field in ("email", "phone"):
        value = (contact.get(field) or "").strip()
        if value and not getattr(profile, field, None):
            setattr(profile, field, value)
            changed[field] = value
    return changed
