"""Recover missing provider categories from stored resumes, then screen unresolved rows.

Run a small read-only sample first::

    python -m app.repair_missing_provider_taxonomy --limit 100

Run the resumable production pass::

    python -m app.repair_missing_provider_taxonomy --apply --workers 12

Only exact healthcare role evidence is accepted. A physician credential alone
establishes the Physician category, but does not invent a medical specialty.
The existing listing state is preserved: visible rows are temporarily screened
and re-listed only when a category is recovered; previously hidden rows stay
hidden. This script never stores resume text in its logs.
"""
from __future__ import annotations

import argparse
import json
import re
import warnings
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from .config import settings
from .importers.parsing import extract_text_from_bytes
from .services import storage


ROOT = Path(__file__).resolve().parent.parent
CHECKPOINT = ROOT / "exports" / "missing_provider_taxonomy_checkpoint.json"
EXPECTED_HOST = "ep-nameless-flower-ah7umr3s-pooler.c-3.us-east-1.aws.neon.tech"
PENDING = "missing_taxonomy_pending"
UNRESOLVED = "missing_taxonomy_unresolved"


@dataclass(frozen=True)
class Role:
    profession: str
    specialty: str | None
    category: str
    expression: str


# Order matters only for closely related roles. These are role titles or
# credentials, not clinical words such as "ICU" or "family medicine" alone.
ROLES = (
    Role("CRNA", "Certified Registered Nurse Anesthetist", "Nursing", r"\b(?:certified registered nurse anesthetist|nurse anesthetist|CRNA)\b"),
    Role("NP", "Nurse Practitioner", "Nursing", r"\b(?:nurse practitioner|FNP-C|FNP-BC|PMHNP-BC|AGACNP|AGNP|ACNP|NP-C)\b"),
    Role("PA", "Physician Assistant", "Physician Assistant", r"\b(?:physician assistant|physician associate|PA-C)\b"),
    Role("RN", "Registered Nurse", "Nursing", r"\b(?:registered nurse|RN)\b"),
    Role("LPN", "Licensed Practical Nurse", "Nursing", r"\b(?:licensed practical nurse|licensed vocational nurse|LPN|LVN)\b"),
    Role("CNA", "Certified Nursing Assistant", "Nursing", r"\b(?:certified nursing assistant|certified nurse aide|CNA)\b"),
    Role("Medical Assistant (MA)", "Medical Assistant", "Allied Health & Technicians", r"\b(?:certified medical assistant|medical assistant|CMA|RMA)\b"),
    Role("Pharmacy Tech", "Pharmacy Technician", "Pharmacy", r"\b(?:pharmacy technician|pharmacy tech|CPhT)\b"),
    Role("Pharmacist", "Pharmacist", "Pharmacy", r"\b(?:pharmacist|PharmD)\b"),
    Role("Physical Therapist (PT)", "Physical Therapist", "Therapy", r"\bphysical therapist\b"),
    Role("Physical Therapy Assistant (PTA)", "Physical Therapy Assistant", "Therapy", r"\b(?:physical therapist assistant|physical therapy assistant|PTA)\b"),
    Role("Occupational Therapist (OT)", "Occupational Therapist", "Therapy", r"\boccupational therapist\b"),
    Role("Certified Occupational Therapist Assistant (COTA)", "Occupational Therapy Assistant", "Therapy", r"\b(?:occupational therapy assistant|occupational therapist assistant|COTA)\b"),
    Role("Registered Respiratory Therapist (RRT)", "Respiratory Therapist", "Allied Health & Technicians", r"\b(?:respiratory therapist|RRT)\b"),
    Role("Licensed Clinical Social Worker (LCSW)", "Clinical Social Worker", "Social Work & Community Services", r"\b(?:licensed clinical social worker|LCSW)\b"),
    Role("Emergency Medical Technician (EMT)", "Emergency Medical Technician", "Allied Health & Technicians", r"\b(?:emergency medical technician|EMT)\b"),
    Role("Surgical Tech", "Surgical Technologist", "Allied Health & Technicians", r"\b(?:surgical technologist|surgical technician|surgical tech)\b"),
    Role("Dialysis Tech", "Dialysis Technician", "Allied Health & Technicians", r"\b(?:dialysis technician|dialysis tech)\b"),
    Role("Sterile Processing Tech", "Sterile Processing Technician", "Allied Health & Technicians", r"\b(?:sterile processing technician|sterile processing tech)\b"),
    Role("MD", None, "Physician", r"\b(?:physician|medical doctor|doctor of medicine)\b|\bM\.D\.\b|\bMD\b"),
    Role("DO", None, "Physician", r"\b(?:osteopathic physician|doctor of osteopathic medicine)\b|\bD\.O\.\b"),
)
PATTERNS = {role.profession: re.compile(role.expression, re.IGNORECASE) for role in ROLES}
PATTERNS["DO"] = re.compile(
    r"(?i:\b(?:osteopathic physician|doctor of osteopathic medicine)\b)"
    r"|(?<![A-Za-z])(?:D\.O\.|DO)(?![A-Za-z])"
)
ALIASES = {
    "bsn": "RN", "msn": "RN", "adn": "RN", "registered nurse": "RN",
    "licensed practical nurse": "LPN", "licensed vocational nurse": "LPN",
    "nurse practitioner": "NP", "physician assistant": "PA",
    "pharmacist": "Pharmacist", "physician": "MD",
}


def _normalized_profession(value: str | None) -> str:
    value = (value or "").strip()
    return ALIASES.get(value.lower(), value)


def _resume_text(url: str) -> str:
    key, local = storage.key_from_url(url)
    raw = storage.download_bytes(key, prefer_local=local)
    if Path(key).suffix.lower() == ".pdf":
        # PyMuPDF is substantially faster than pypdf for this large batch.
        # The PDF bytes remain in memory and are never written to disk.
        try:
            import fitz
        except ImportError:
            pass
        else:
            with fitz.open(stream=raw, filetype="pdf") as doc:
                return "\n".join(page.get_text("text") for page in doc)
    return extract_text_from_bytes(raw, Path(key).name)


def classify(row: dict, resume_text: str) -> tuple[str | None, str | None, str]:
    """Return (specialty, job_category, evidence code), never guessing a role."""
    if len(resume_text.strip()) < 40:
        return None, None, "empty_text"
    lines = [line.strip() for line in resume_text.splitlines() if line.strip()]
    header = "\n".join(lines[:25])[:2500]
    structured = _normalized_profession(row.get("profession_type"))
    positioned = [(role, PATTERNS[role.profession].search(header).start())
                  for role in ROLES if PATTERNS[role.profession].search(header)]
    matches = [role for role, _ in positioned]
    matching_structured = [role for role in matches if role.profession == structured]
    first_role = min(positioned, key=lambda match: match[1])[0] if positioned else None
    if len(matching_structured) == 1 and first_role == matching_structured[0]:
        role = matching_structured[0]
        return role.specialty, role.category, "role_header_and_profession"
    if structured and not matches:
        matching_full = [role for role in ROLES if role.profession == structured and PATTERNS[role.profession].search(resume_text)]
        if len(matching_full) == 1:
            role = matching_full[0]
            return role.specialty, role.category, "role_resume_and_profession"
    if not structured and len(matches) == 1:
        role = matches[0]
        return role.specialty, role.category, "unique_header_role"
    return None, None, "ambiguous_or_no_role"


def _one(row: dict) -> tuple[dict, str | None, str | None, str]:
    try:
        specialty, category, evidence = classify(row, _resume_text(row["resume_url"]))
        return row, specialty, category, evidence
    except Exception as exc:  # unreadable documents stay screened
        return row, None, None, f"read_error_{type(exc).__name__}"


SELECT = text("""
    SELECT profile_id::text, resume_url, profession_type, specialty, headline,
           is_listable, screen_reason
      FROM profiles
     WHERE (job_category IS NULL OR btrim(job_category) = '')
       AND profile_id > :cursor
     ORDER BY profile_id
     LIMIT :batch_size
""")
UPDATE_RESOLVED = text("""
    WITH incoming AS (
        SELECT * FROM jsonb_to_recordset(CAST(:rows AS jsonb))
        AS x(profile_id text, resume_url text, specialty text, category text)
    )
    UPDATE profiles AS p
       SET specialty = CASE WHEN NULLIF(btrim(p.specialty),'') IS NULL
                            THEN i.specialty ELSE p.specialty END,
           job_category = i.category,
           is_listable = CASE WHEN p.screen_reason = :pending THEN true ELSE p.is_listable END,
           screen_reason = CASE WHEN p.screen_reason = :pending THEN NULL ELSE p.screen_reason END,
           screened_at = now(), updated_at = now()
      FROM incoming i
     WHERE p.profile_id = i.profile_id
       AND (p.job_category IS NULL OR btrim(p.job_category) = '')
       AND p.resume_url IS NOT DISTINCT FROM i.resume_url
    RETURNING p.profile_id
""")
UPDATE_UNRESOLVED = text("""
    WITH incoming AS (
        SELECT * FROM jsonb_to_recordset(CAST(:rows AS jsonb))
        AS x(profile_id text, resume_url text)
    )
    UPDATE profiles AS p
       SET is_listable = false,
           screen_reason = CASE WHEN p.screen_reason = :pending THEN :unresolved
                                ELSE p.screen_reason END,
           screened_at = now(), updated_at = now()
      FROM incoming i
     WHERE p.profile_id = i.profile_id
       AND (p.job_category IS NULL OR btrim(p.job_category) = '')
       AND p.resume_url IS NOT DISTINCT FROM i.resume_url
    RETURNING p.profile_id
""")


def _checkpoint(cursor: str, stats: Counter, complete: bool = False) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHECKPOINT.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last_profile_id": cursor, "updated_at": datetime.now(timezone.utc).isoformat(),
                               "complete": complete, "stats": dict(stats)}, indent=2), encoding="utf-8")
    tmp.replace(CHECKPOINT)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    url = make_url(settings.database_url)
    if url.host != EXPECTED_HOST or url.database != "neondb":
        raise RuntimeError("Unexpected database target; production cleanup refused")
    if args.workers < 1 or args.workers > 24 or args.batch_size < 1 or args.batch_size > 1000:
        raise ValueError("workers must be 1..24 and batch size 1..1000")
    cursor = "00000000-0000-0000-0000-000000000000"
    stats: Counter = Counter()
    if args.apply and CHECKPOINT.exists() and not args.restart:
        saved = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
        cursor = saved.get("last_profile_id", cursor)
        stats.update(saved.get("stats", {}))
    engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args={"connect_timeout": 8})
    if args.apply and cursor == "00000000-0000-0000-0000-000000000000":
        # The reason marker remembers which profiles were visible before this
        # pass, so only those may become visible again after successful repair.
        while True:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL statement_timeout = '120s'"))
                result = conn.execute(text("""
                    WITH targets AS (
                        SELECT profile_id FROM profiles
                         WHERE (job_category IS NULL OR btrim(job_category) = '')
                           AND is_listable = true
                         ORDER BY profile_id LIMIT 5000
                    )
                    UPDATE profiles p SET is_listable = false,
                        screen_reason = :pending, screened_at = now(), updated_at = now()
                      FROM targets t WHERE p.profile_id = t.profile_id
                """), {"pending": PENDING})
                changed = result.rowcount
                stats["prehidden"] += changed
            _checkpoint(cursor, stats)
            print(f"Screened {stats['prehidden']} visible uncategorized profiles.", flush=True)
            if changed < 5000:
                break
    processed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        while True:
            with engine.connect() as conn:
                rows = [dict(r) for r in conn.execute(SELECT, {"cursor": cursor, "batch_size": min(args.batch_size, args.limit-processed) if args.limit else args.batch_size}).mappings()]
            if not rows:
                break
            results = list(pool.map(_one, rows))
            if args.apply:
                with engine.begin() as conn:
                    conn.execute(text("SET LOCAL statement_timeout = '120s'"))
                    resolved_rows = [{"profile_id": row["profile_id"], "resume_url": row["resume_url"],
                                      "specialty": specialty, "category": category}
                                     for row, specialty, category, _ in results if category]
                    unresolved_rows = [{"profile_id": row["profile_id"], "resume_url": row["resume_url"]}
                                       for row, _, category, _ in results if not category]
                    changed_ids = set()
                    if resolved_rows:
                        changed_ids.update(str(r[0]) for r in conn.execute(UPDATE_RESOLVED, {
                            "rows": json.dumps(resolved_rows), "pending": PENDING}))
                    if unresolved_rows:
                        changed_ids.update(str(r[0]) for r in conn.execute(UPDATE_UNRESOLVED, {
                            "rows": json.dumps(unresolved_rows), "pending": PENDING,
                            "unresolved": UNRESOLVED}))
                    for row, specialty, category, evidence in results:
                        if row["profile_id"] in changed_ids:
                            stats[f"evidence_{evidence}"] += 1
                            stats["resolved" if category else "unresolved"] += 1
                            if category:
                                stats[f"category_{category}"] += 1
                            if row["screen_reason"] == PENDING and category:
                                stats["relisted"] += 1
                    stats["processed"] += len(results)
                cursor = rows[-1]["profile_id"]
                _checkpoint(cursor, stats)
            else:
                for _, _, category, evidence in results:
                    stats["resolved" if category else "unresolved"] += 1
                    stats[f"evidence_{evidence}"] += 1
                cursor = rows[-1]["profile_id"]
            processed += len(rows)
            print(json.dumps({"cursor": cursor, "processed_this_run": processed,
                              "resolved": stats["resolved"], "unresolved": stats["unresolved"]}), flush=True)
            if args.limit and processed >= args.limit:
                break
    if args.apply and (not args.limit or processed < args.limit):
        _checkpoint(cursor, stats, complete=True)
    print(json.dumps({"complete": not args.limit or processed < args.limit, "stats": dict(stats)}, indent=2), flush=True)


if __name__ == "__main__":
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    main()
