"""Normalize provider profile-card fields from stored resumes.

Run from the project root:

    .venv\\Scripts\\python -m app.format_provider_profiles --dry-run --limit 25
    .venv\\Scripts\\python -m app.format_provider_profiles

The command re-reads each stored resume, parses it with the formatter in
app.importers.parsing, then updates the fields shown on provider cards:
name, title/license, category, specialty/headline, city/state, experience, and
missing contact details.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import select

from .database import SessionLocal
from .importers.parsing import (
    _bad_name,
    _clean_state,
    _is_noisy_card_text,
    _title_city,
    extract_text_from_bytes,
    parse_resume,
)
from .models import Profile
from .routers.profiles import _compute_completion
from .services import storage


DISPLAY_FIELDS = (
    "headline",
    "specialty",
    "profession_type",
    "provider_category",
    "city",
    "state_code",
)
CONTACT_FIELDS = ("email", "phone")


def _blank(value) -> bool:
    return value is None or str(value).strip() == ""


def _same(value, other) -> bool:
    return str(value or "").strip() == str(other or "").strip()


def _clean_existing_city(value: str | None) -> str | None:
    return _title_city(value)


def _should_update_text(current, new, *, overwrite: bool = False) -> bool:
    if _blank(new):
        return False
    if _same(current, new):
        return False
    if overwrite or _blank(current) or _is_noisy_card_text(current):
        return True
    if isinstance(current, str) and current.isupper() and _same(current.title(), new):
        return True
    return False


def _apply_fields(profile: Profile, fields: dict, *, overwrite_card: bool, overwrite_contact: bool) -> dict[str, tuple]:
    changes: dict[str, tuple] = {}

    first = fields.get("first_name")
    last = fields.get("last_name")
    if first and last and (overwrite_card or _bad_name(profile.first_name, profile.last_name)):
        if not (_same(profile.first_name, first) and _same(profile.last_name, last)):
            changes["name"] = (f"{profile.first_name} {profile.last_name}", f"{first} {last}")
            profile.first_name = first[:100]
            profile.last_name = last[:100]

    for field in DISPLAY_FIELDS:
        new = fields.get(field)
        current = getattr(profile, field, None)
        if field == "city":
            new = _clean_existing_city(new)
            if _blank(new) and current and (not _clean_existing_city(current) or _is_noisy_card_text(current)):
                changes[field] = (current, None)
                setattr(profile, field, None)
                continue
        elif field == "state_code":
            new = _clean_state(new)
        if _should_update_text(current, new, overwrite=overwrite_card):
            changes[field] = (current, new)
            setattr(profile, field, new)

    years = int(fields.get("years_experience") or 0)
    if years and (overwrite_card or not profile.years_experience or profile.years_experience > 60):
        if profile.years_experience != years:
            changes["years_experience"] = (profile.years_experience, years)
            profile.years_experience = years

    contact_locked = bool(profile.contact_updated_by_email)
    for field in CONTACT_FIELDS:
        new = fields.get(field)
        current = getattr(profile, field, None)
        if new and (overwrite_contact or (not contact_locked and _blank(current))):
            if not _same(current, new):
                changes[field] = (current, new)
                setattr(profile, field, new)

    if changes:
        profile.rebuild_search_text()
        profile.completion_score = _compute_completion(profile)
    return changes


def _profile_text(profile: Profile) -> tuple[str, str]:
    key, is_local = storage.key_from_url(profile.resume_url or "")
    data = storage.download_bytes(key, prefer_local=is_local)
    return extract_text_from_bytes(data, Path(key).name), key


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize provider card fields from stored resumes.")
    parser.add_argument("--dry-run", action="store_true", help="Show planned changes without saving")
    parser.add_argument("--limit", type=int, default=0, help="Maximum profiles to process")
    parser.add_argument("--offset", type=int, default=0, help="Profiles to skip")
    parser.add_argument("--commit-every", type=int, default=100, help="Commit batch size")
    parser.add_argument("--overwrite-card-fields", action="store_true", help="Replace existing card fields with parsed values")
    parser.add_argument("--overwrite-contact", action="store_true", help="Overwrite email/phone even if already present")
    parser.add_argument("--source", default="", help="Optional source filter, e.g. resume_parse")
    args = parser.parse_args()

    db = SessionLocal()
    processed = updated = failed = 0
    try:
        stmt = select(Profile).where(Profile.resume_url.isnot(None)).order_by(Profile.created_at.desc())
        if args.source:
            stmt = stmt.where(Profile.source == args.source)
        if args.offset:
            stmt = stmt.offset(args.offset)
        if args.limit:
            stmt = stmt.limit(args.limit)

        for profile in db.scalars(stmt).all():
            processed += 1
            try:
                text, key = _profile_text(profile)
                fields = parse_resume(text, Path(key))
                changes = _apply_fields(
                    profile,
                    fields,
                    overwrite_card=args.overwrite_card_fields,
                    overwrite_contact=args.overwrite_contact,
                )
                if changes:
                    updated += 1
                    sample = ", ".join(
                        f"{field}: {old!r} -> {new!r}"
                        for field, (old, new) in list(changes.items())[:5]
                    )
                    print(f"{profile.profile_id} {profile.first_name} {profile.last_name}: {sample}")
                if not args.dry_run and processed % max(args.commit_every, 1) == 0:
                    db.commit()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAILED {profile.profile_id}: {exc}")

        if args.dry_run:
            db.rollback()
        else:
            db.commit()
        print(f"\nProcessed={processed} Updated={updated} Failed={failed} DryRun={args.dry_run}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
