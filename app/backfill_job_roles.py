"""Derive `profession_type` and `specialty` for job postings from their titles.

The imported reqs carry a free-text title ("Registered Nurse - Labor and
Delivery") but no structured role, so the matching engine has nothing to filter
on. This fills both columns using the SAME vocabulary the profiles use — an
exact-match filter against a value no profile holds would silently match zero
candidates, so a term is only written when profiles actually use it.

Run:  python -m app.backfill_job_roles [--dry-run]
"""
from __future__ import annotations

import argparse
import re

from sqlalchemy import func, select

from .database import SessionLocal
from .models import JobPosting, Profile

# Ordered longest/most-specific first: "Physical Therapist Assistant" must be
# tested before "Physical Therapist", "Nursing Assistant" before "Nurse".
_ROLE_RULES: list[tuple[str, str]] = [
    (r"physical\s+therap(y|ist)\s+assistant|\bPTA\b", "PTA"),
    (r"occupational\s+therap(y|ist)\s+assistant|\bCOTA\b|\bOTA\b", "OTA"),
    (r"speech[\s-]language|speech\s+therap|\bSLP\b", "SLP"),
    (r"nurse\s+anesthetist|\bCRNA\b", "CRNA"),
    (r"nurse\s+practitioner|\bNP\b", "NP"),
    (r"nurs(ing|e)\s+assistant|nurse\s+aide|\bCNA\b|\bGNA\b|\bLNA\b", "CNA"),
    (r"licensed\s+practical\s+nurse|\bLPN\b", "LPN"),
    (r"licensed\s+vocational\s+nurse|\bLVN\b", "LVN"),
    (r"registered\s+nurse|\bRN\b", "RN"),
    (r"physician\s+assistant|\bPA-?C?\b", "PA"),
    (r"physical\s+therap(y|ist)|\bPT\b", "PT"),
    (r"occupational\s+therap(y|ist)|\bOT\b", "OT"),
    (r"respiratory\s+therap(y|ist)|\bRT\b", "RT"),
    (r"nuclear\s+medicine", "Nuclear Medicine Technologist"),
    (r"radiation\s+therapist", "Radiation Therapist"),
    (r"\bCT\s+tech", "CT Technologist"),
    (r"\bMRI\s+tech", "MRI Technologist"),
    (r"ultrasound|sonograph", "Ultrasound Technologist"),
    (r"x-?ray|radiologic\s+tech", "Radiologic Technologist"),
    (r"surgical\s+tech|\bOR\s+tech|scrub\s+tech", "Surgical Technologist"),
    (r"sterile\s+processing", "Sterile Processing Tech"),
    (r"paramedic", "Paramedic"),
    (r"pharmacist|\bPharmD\b", "PharmD"),
    (r"\bphysician\b|\bMD\b", "MD"),
]

# Specialty terms, expressed in the profiles' own vocabulary.
_SPECIALTY_RULES: list[tuple[str, str]] = [
    (r"labor\s+(and|&)\s+delivery|\bL\s*&\s*D\b", "Labor & Delivery"),
    (r"\bNICU\b|neonatal", "NICU"),
    (r"\bPACU\b|post[\s-]anesthesia", "PACU"),
    (r"\bICU\b|intensive\s+care|critical\s+care|stepdown|step[\s-]down", "ICU"),
    (r"\bER\b|\bED\b|emergency", "ER"),
    (r"operating\s+room|\bOR\b|periop|surgical\s+services", "OR"),
    (r"cath\s+lab|catheter", "Cath Lab"),
    (r"med[\s/-]*surg|medical[\s/-]surgical", "Med-Surg"),
    (r"tele(metry)?\b|\bMS/Tele\b", "Telemetry"),
    (r"oncology|\bcancer\b", "Oncology"),
    (r"dialysis|nephrology", "Dialysis"),
    (r"psych(iatric)?\b|behavioral\s+health", "Psych"),
    (r"home\s+health|home\s+care", "Home Health"),
    (r"pediatric|\bpeds\b", "Pediatrics"),
    (r"case\s+management", "Case Management"),
    (r"phlebotom", "Phlebotomy"),
    (r"cardiology|cardiac", "Cardiology"),
]


def _first_match(title: str, rules: list[tuple[str, str]]) -> str | None:
    for pattern, value in rules:
        if re.search(pattern, title, re.I):
            return value
    return None


def run(dry_run: bool = False) -> None:
    db = SessionLocal()
    try:
        # Only write vocabulary the profiles actually use, otherwise the
        # engine's exact-match filter would match nobody.
        known_prof = {
            p for (p,) in db.execute(
                select(Profile.profession_type)
                .where(Profile.profession_type.isnot(None))
                .group_by(Profile.profession_type)
                .having(func.count() >= 5)
            ).all() if p
        }
        known_spec = {
            s for (s,) in db.execute(
                select(Profile.specialty)
                .where(Profile.specialty.isnot(None))
                .group_by(Profile.specialty)
                .having(func.count() >= 5)
            ).all() if s
        }
        print(f"vocabulary: {len(known_prof)} professions, {len(known_spec)} specialties")

        jobs = db.scalars(select(JobPosting)).all()
        prof_set = spec_set = skipped_prof = skipped_spec = 0
        for job in jobs:
            title = job.title or ""
            if not job.profession_type:
                role = _first_match(title, _ROLE_RULES)
                if role and role in known_prof:
                    job.profession_type = role
                    prof_set += 1
                elif role:
                    skipped_prof += 1     # no candidates hold this credential
            if not job.specialty:
                spec = _first_match(title, _SPECIALTY_RULES)
                if spec and spec in known_spec:
                    job.specialty = spec
                    spec_set += 1
                elif spec:
                    skipped_spec += 1

        if dry_run:
            db.rollback()
            print("DRY RUN — nothing written")
        else:
            db.commit()
        print(f"jobs={len(jobs)}  profession_type set={prof_set} (skipped {skipped_prof} "
              f"unknown)  specialty set={spec_set} (skipped {skipped_spec} unknown)")

        if not dry_run:
            rows = db.execute(
                select(JobPosting.profession_type, JobPosting.specialty, func.count())
                .group_by(JobPosting.profession_type, JobPosting.specialty)
                .order_by(func.count().desc()).limit(12)
            ).all()
            print("\ntop role/specialty combos now on jobs:")
            for prof, spec, n in rows:
                print(f"  {n:4d}  {prof or '-':28s} {spec or '-'}")
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run(dry_run=a.dry_run)
