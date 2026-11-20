"""Repair credential-as-surname records only when the source header proves the name.

Reads the Data tab audit IDs, then checks each current Neon row and its stored
resume header. Changes are transactional, idempotent, and logged in a DB audit
table. A dry run is the default; use --apply to write. No visibility is changed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.resume_cleanup_common import database_url  # noqa: E402

ROLE = {
    "MD": "Physician", "DO": "Physician", "MBBS": "Physician",
    "RN": "Nursing", "LPN": "Nursing", "LVN": "Nursing", "CNA": "Nursing",
    "NP": "APP", "FNP": "APP", "APRN": "APP", "CRNA": "APP", "CNM": "APP", "PA-C": "APP",
    "DPT": "Therapy", "PT": "Therapy", "OT": "Therapy", "OTR": "Therapy",
    "PHARMD": "Pharmacy", "DDS": "Dental", "DMD": "Dental", "DPM": "Podiatry",
}
NONCLINICAL_SUFFIX = {"MBA", "MS", "MA", "PHD", "MPH", "MSN", "BSN"}
SUFFIXES = set(ROLE) | NONCLINICAL_SUFFIX
NAME_PART = re.compile(r"^[A-Za-z][A-Za-z'-]*\.?$")
VERSION = "resume_header_name_20261120_v1"


def proposal(first: str | None, last: str | None, category: str | None,
             header: str | None) -> tuple[str, str] | None:
    """Return (surname, credential) when first-line identity is unambiguous."""
    if (not first or not last or last.upper().strip(". ") not in ROLE or not header
            or first.casefold().strip(".") in {"dr", "mr", "mrs", "ms", "miss"}
            or not NAME_PART.fullmatch(first)):
        return None
    line = next((part.strip() for part in header.splitlines() if part.strip()), "")
    if not line or len(line) > 100 or any(ch.isdigit() for ch in line):
        return None
    line = re.sub(r"^(?:Dr\.?|Mr\.?|Ms\.?)\s+", "", line, flags=re.I)
    words = line.replace(",", " ").split()
    suffixes = []
    while words and words[-1].upper().strip(".") in SUFFIXES:
        suffixes.append(words.pop().upper().strip("."))
    roles = {token for token in suffixes if token in ROLE}
    if len(roles) != 1 or len(words) < 2 or len(words) > 5:
        return None
    role = next(iter(roles))
    if ROLE[role] != category or words[0].casefold().strip(".") != first.casefold().strip("."):
        return None
    surname = words[-1].strip(".")
    if (not NAME_PART.fullmatch(surname) or len(surname) < 2
            or surname.upper() in SUFFIXES or surname.casefold() == last.casefold()):
        return None
    if not all(NAME_PART.fullmatch(part) for part in words):
        return None
    return (surname.title() if surname.isupper() else surname), role


def audited_ids(path: Path):
    seen = set()
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if "credential_as_surname" not in row.get("flags", []):
                continue
            key = row["provider_id"]
            if key not in seen:
                seen.add(key)
                yield key


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env-file", type=Path)
    ap.add_argument("--audit-file", type=Path, default=ROOT / "reports/data_tab_resume_audit_20261119.jsonl")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="Maximum flagged IDs for a pilot")
    args = ap.parse_args()
    url = database_url(args.env_file)
    stats = {"flagged": 0, "eligible": 0, "updated": 0}
    with psycopg.connect(url, connect_timeout=20) as db:
        if args.apply:
            with db.cursor() as cur:
                cur.execute('''CREATE TABLE IF NOT EXISTS public."New_provider_resume_name_cleanup_audit" (
                    provider_id uuid NOT NULL, cleanup_version text NOT NULL,
                    previous_name text NOT NULL, corrected_name text NOT NULL,
                    previous_credential text, corrected_credential text NOT NULL,
                    source_sha256 char(64), changed_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (provider_id, cleanup_version))''')
            db.commit()
        batch = []
        for key in audited_ids(args.audit_file):
            batch.append(key)
            if len(batch) >= 200:
                process_batch(db, batch, args.apply, stats)
                batch.clear()
            if args.limit and stats["flagged"] + len(batch) >= args.limit:
                break
        if batch:
            process_batch(db, batch, args.apply, stats)
    print(json.dumps({"complete": True, "apply": args.apply, **stats}), flush=True)


def process_batch(db, ids: list[str], apply: bool, stats: dict) -> None:
    stats["flagged"] += len(ids)
    with db.cursor() as cur:
        cur.execute('''SELECT provider_id::text, first_name, last_name, credential,
                              job_category, left(extracted_text, 250), file_sha256
                       FROM public."New_provider"
                       WHERE provider_id = ANY(%s::uuid[]) AND parse_status = 'parsed'
                         AND source_kind IN ('resume','generated_cv')
                         AND review_status <> 'rejected' ''', (ids,))
        rows = cur.fetchall()
    for pid, first, last, credential, category, header, sha in rows:
        change = proposal(first, last, category, header)
        if not change:
            continue
        surname, role = change
        stats["eligible"] += 1
        if not apply:
            continue
        with db.cursor() as cur:
            cur.execute('''UPDATE public."New_provider" SET last_name=%s, credential=%s,
                            quality_flags=CASE WHEN 'resume_header_name_corrected'=ANY(quality_flags)
                                THEN quality_flags ELSE array_append(quality_flags,'resume_header_name_corrected') END,
                            updated_at=now()
                           WHERE provider_id=%s::uuid AND last_name=%s AND file_sha256 IS NOT DISTINCT FROM %s
                             AND parse_status='parsed' AND review_status <> 'rejected'
                           RETURNING provider_id''', (surname, role, pid, last, sha))
            if not cur.fetchone():
                continue
            cur.execute('''INSERT INTO public."New_provider_resume_name_cleanup_audit"
                           (provider_id,cleanup_version,previous_name,corrected_name,
                            previous_credential,corrected_credential,source_sha256)
                           VALUES (%s::uuid,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                        (pid, VERSION, f"{first} {last}", f"{first} {surname}", credential, role, sha))
            stats["updated"] += 1
    db.commit()
    print(json.dumps(stats), flush=True)


if __name__ == "__main__":
    main()
