"""Bulk-import a folder of resumes into candidate profiles.

Usage:
    python -m app.importers.resumes "C:\\path\\to\\resumes"
    python -m app.importers.resumes "C:\\path\\to\\resumes" --dry-run

For every .pdf / .docx file it:
  1. stores the original file (S3 in prod, ./uploads locally) -> resume_url
  2. extracts name / contact / specialty / profession / certs / state
  3. creates a Profile (source=resume_parse) + Certification rows + a skill

Re-running is safe: a file whose resume already exists (by stored filename) is
skipped. Imported profiles have no login account (user_id = NULL) until a person
registers and claims them.
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from datetime import date

from ..database import SessionLocal, init_db
from ..models import Certification, License, Profile, ProfileSkill
from ..models.enums import LicenseStatus, ProfileSource
from ..services import storage
from .parsing import extract_text, is_real_name, parse_resume

SUPPORTED = {".pdf", ".docx"}


def import_folder(folder: str, dry_run: bool = False) -> dict:
    root = Path(folder).expanduser()
    if not root.is_dir():
        print(f"ERROR: not a folder: {root}", file=sys.stderr)
        return {"error": "not_a_folder"}

    files = sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED
    )
    print(f"Found {len(files)} resume file(s) under {root}")

    if not dry_run:
        init_db()
    db = SessionLocal() if not dry_run else None

    created = skipped = failed = 0
    try:
        for path in files:
            try:
                text = extract_text(path)
                fields = parse_resume(text, path)
            except Exception as exc:  # noqa: BLE001
                print(f"  SKIP (parse error) {path.name}: {exc}")
                failed += 1
                continue

            label = f"{fields['first_name']} {fields['last_name']}"
            if dry_run:
                print(f"  WOULD IMPORT  {path.name:40.40s} -> "
                      f"{label} | {fields.get('profession_type') or '?'} "
                      f"{fields.get('specialty') or ''} | "
                      f"certs={','.join(fields['certifications']) or '-'} | "
                      f"{fields.get('state_code') or '?'}")
                created += 1
                continue

            # Store the original file -> resume_url
            key = storage.build_key("resumes/import", path.name)
            content_type = ("application/pdf" if path.suffix.lower() == ".pdf"
                            else "application/vnd.openxmlformats-officedocument."
                                 "wordprocessingml.document")
            with open(path, "rb") as fh:
                resume_url = storage.upload(io.BytesIO(fh.read()), key, content_type)

            profile = Profile(
                user_id=None,
                first_name=fields["first_name"],
                last_name=fields["last_name"],
                headline=fields["headline"],
                bio=fields.get("bio"),
                phone=fields.get("phone"),
                email=fields.get("email"),
                specialty=fields["specialty"],
                profession_type=fields["profession_type"],
                provider_category=fields.get("provider_category"),
                american_board=fields.get("american_board"),
                is_listable=is_real_name(fields["first_name"], fields["last_name"]),
                years_experience=fields["years_experience"],
                city=fields.get("city"),
                state_code=fields["state_code"],
                npi_number=_unique_npi(db, fields["npi_number"]),
                resume_url=resume_url,
                open_to_work=True,
                source=ProfileSource.resume_parse,
                completion_score=_completion(fields),
            )
            profile.rebuild_search_text()
            db.add(profile)
            db.flush()

            for cert in fields["certifications"]:
                db.add(Certification(profile_id=profile.profile_id, cert_name=cert[:100]))
            for lic in fields.get("licenses", []):
                expiry = date(lic["expiry_year"], 12, 31) if lic.get("expiry_year") else None
                db.add(License(
                    profile_id=profile.profile_id,
                    license_type=lic.get("license_type") or "MD",
                    license_number="(from resume)",
                    state_code=lic["state_code"],
                    status=LicenseStatus.active,
                    expiry_date=expiry,
                    verification_source="resume",
                ))
            if fields["specialty"]:
                db.add(ProfileSkill(profile_id=profile.profile_id,
                                    name=fields["specialty"][:100],
                                    years=fields["years_experience"] or None))
            db.commit()
            created += 1
            print(f"  IMPORTED  {label:28.28s} <- {path.name}")
        return {"created": created, "skipped": skipped, "failed": failed,
                "total": len(files)}
    finally:
        if db is not None:
            db.close()


def _unique_npi(db, npi: str | None) -> str | None:
    """Avoid violating the unique NPI constraint on duplicate/parsed values."""
    if not npi:
        return None
    exists = db.query(Profile).filter(Profile.npi_number == npi).first()
    return None if exists else npi


def _completion(fields: dict) -> int:
    score = 20  # has a resume on file
    if fields["specialty"]:
        score += 15
    if fields["profession_type"]:
        score += 10
    if fields.get("city"):
        score += 5
    if fields["years_experience"]:
        score += 5
    if fields["state_code"]:
        score += 10
    if fields["certifications"]:
        score += 10
    if fields.get("licenses"):
        score += 10
    if fields.get("bio"):
        score += 5
    if fields["email"]:
        score += 10
    return min(score, 100)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import resumes into candidate profiles")
    parser.add_argument("folder", help="Path to a folder of .pdf/.docx resumes")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and preview without writing to the database")
    args = parser.parse_args()

    result = import_folder(args.folder, dry_run=args.dry_run)
    print("\nSummary:", result)


if __name__ == "__main__":
    main()
