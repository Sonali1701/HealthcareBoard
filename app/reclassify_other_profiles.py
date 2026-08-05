"""Reclassify ``Other``/blank profiles from their stored resumes.

The taxonomy is intentionally narrow and mirrors the Providers directory:

* Physicians: MD / Doctor of Medicine / Family Medicine
* Allied: radiology, X-ray, CT, MRI, mammography, ultrasound/sonography,
  echo, vascular, nuclear medicine, IR, and cardiac cath lab technologists
* APP: Nurse Practitioner and CRNA
* Nursing: RN, LPN, and CNA

Only profiles currently categorized as ``Other`` or NULL are considered.
Hidden profiles remain hidden. Unmatched resumes remain ``Other``. The command
is a dry run unless ``--apply`` is supplied, and commits in small batches so an
interrupted production run can be resumed safely.

Examples:

    python -m app.reclassify_other_profiles --limit 200
    python -m app.reclassify_other_profiles --apply --workers 16
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import or_, select, text as sql_text

from .database import SessionLocal
from .importers.parsing import extract_text_from_bytes
from .models import Profile
from .services import storage


@dataclass(frozen=True)
class Decision:
    category: str
    profession: str
    evidence: str


_CRNA = re.compile(
    r"\b(?:CRNA|certified registered nurse anesthetist|nurse anesthetist)\b", re.I
)
_NP = re.compile(
    r"\b(?:nurse practitioner|NP-C|FNP(?:-C|-BC)?|PMHNP(?:-BC)?|AGNP|ACNP|"
    r"AGACNP|PNP|WHNP)\b",
    re.I,
)
_NP_BARE = re.compile(r"\bNP\b", re.I)
_MD_PHRASE = re.compile(r"\b(?:medical doctor|doctor of medicine)\b", re.I)
_MD_CREDENTIAL = re.compile(
    r"(?im)(?:^[^\n]{1,100},\s*M\.?D\.?(?:\s|,|$)|\bM\.D\.\b)"
)
_FAMILY_MEDICINE = re.compile(r"\bfamily medicine\b", re.I)

_ALLIED: tuple[tuple[str, re.Pattern], ...] = (
    ("Cardiac Cath Lab Technologist", re.compile(
        r"\b(?:cardiac\s+)?cath(?:eterization)?\s+lab\s+tech(?:nologist|nician)?\b", re.I)),
    ("Interventional Radiology Technologist", re.compile(
        r"\b(?:interventional radiology|IR)\s+tech(?:nologist|nician)?\b", re.I)),
    ("Nuclear Medicine Technologist", re.compile(
        r"\bnuclear medicine\s+tech(?:nologist|nician)?\b", re.I)),
    ("Mammography Technologist", re.compile(
        r"\b(?:mammography\s+tech(?:nologist|nician)?|mammographer)\b", re.I)),
    ("Ultrasound Technologist", re.compile(
        r"\b(?:ultrasound\s+tech(?:nologist|nician)?|diagnostic medical sonographer|sonographer)\b", re.I)),
    ("Echo Technologist", re.compile(
        r"\b(?:echo(?:cardiography)?\s+tech(?:nologist|nician)?|echocardiographer)\b", re.I)),
    ("Vascular Technologist", re.compile(
        r"\bvascular\s+tech(?:nologist|nician)?\b", re.I)),
    ("MRI Technologist", re.compile(
        r"\bMRI\s+tech(?:nologist|nician)?\b", re.I)),
    ("CT Technologist", re.compile(
        r"\b(?:CT|computed tomography)\s+tech(?:nologist|nician)?\b", re.I)),
    ("X-Ray Technologist", re.compile(
        r"\bX[ -]?ray\s+tech(?:nologist|nician)?\b", re.I)),
    ("Radiologic Technologist", re.compile(
        r"\b(?:radiologic|radiology)\s+tech(?:nologist|nician)?\b|\brad tech\b|\bradiographer\b", re.I)),
)

_REGISTERED_NURSE = re.compile(r"\bregistered nurse\b", re.I)
_RN = re.compile(r"\bRN\b", re.I)
_LPN = re.compile(r"\b(?:LPN|licensed practical nurse)\b", re.I)
_CNA = re.compile(r"\b(?:CNA|certified nursing assistant)\b", re.I)
_CREDENTIAL_CONTEXT = re.compile(
    r"\b(?:license|licensure|licensed|certification|certified|credential|degree)\b", re.I
)

# Nursing-unit specialties requested by the board owner. These are deliberately
# matched against résumé text, not the stored specialty field: older imports used
# substring matching and could mistake the "icu" inside "curriculum" for ICU.
_NURSING_SPECIALTIES: tuple[tuple[str, re.Pattern], ...] = (
    ("ICU", re.compile(r"\b(?:ICU|intensive care|critical care|CCU|SICU|MICU)\b", re.I)),
    ("ER", re.compile(r"\bER\b|\bemergency (?:department|room|care)\b", re.I)),
    ("PICU", re.compile(r"\bPICU\b|\bpediatric intensive care\b", re.I)),
    ("NICU", re.compile(r"\bNICU\b|\bneonatal intensive care\b", re.I)),
    ("Labor & Delivery", re.compile(
        r"\b(?:labor (?:and|&) delivery|L&D|postpartum|antepartum|mother[ -]baby)\b", re.I)),
    ("Med-Surg", re.compile(r"\b(?:med[ -]?surg|medical[ -]surgical)\b", re.I)),
    ("Telemetry", re.compile(r"\b(?:telemetry|step[ -]?down|PCU)\b", re.I)),
    ("Oncology", re.compile(r"\b(?:oncology|hematology|chemotherapy)\b", re.I)),
    ("PACU", re.compile(r"\bPACU\b|\bpost[ -]?anesthesia care\b", re.I)),
    ("Operating Room", re.compile(r"\b(?:operating room|perioperative)\b", re.I)),
    ("Dialysis", re.compile(r"\b(?:dialysis|hemodialysis)\b", re.I)),
)
_NURSING_SPECIALTY_CODES = {"", "RN", "LPN", "CNA", "BSN", "MSN", "ADN"}
_CONFLICTING_NON_NURSING_ROLE = re.compile(
    r"\b(?:physician assistant|physician associate|PA-C|doctor of osteopathic|"
    r"pharmacist|PharmD|pharmacy technician|physical therapist|occupational therapist|"
    r"respiratory therapist|speech[ -]language pathologist|social worker|"
    r"medical assistant|dental assistant|radiation therapist|laboratory technologist)\b",
    re.I,
)


def _hits(pattern: re.Pattern, value: str) -> int:
    return sum(1 for _ in pattern.finditer(value or ""))


def _strong(pattern: re.Pattern, structured: str, header: str,
            credential_lines: str, full_text: str, *, full_hits: int = 2) -> bool:
    return bool(
        pattern.search(structured)
        or pattern.search(header)
        or pattern.search(credential_lines)
        or _hits(pattern, full_text) >= full_hits
    )


def _nursing_specialty_fallback(
    full_text: str,
    header: str,
    profession_type: str | None,
) -> Decision | None:
    """Infer RN only from genuine nursing-unit text with no role conflict."""
    code = (profession_type or "").strip().upper().strip(".")
    if code not in _NURSING_SPECIALTY_CODES:
        return None
    if _CONFLICTING_NON_NURSING_ROLE.search(header):
        return None
    for specialty, pattern in _NURSING_SPECIALTIES:
        if pattern.search(header) or _hits(pattern, full_text) >= 2:
            return Decision("Nursing", "RN", f"Nursing specialty: {specialty}")
    return None


def classify_resume_role(
    resume_text: str,
    *,
    profession_type: str | None = None,
    specialty: str | None = None,
    headline: str | None = None,
) -> Decision | None:
    """Return one evidence-backed taxonomy decision, or ``None``.

    Specific roles take precedence over generic nursing history: NP/CRNA first,
    then an MD credential, then an explicit technologist title, then RN/LPN/CNA.
    Family Medicine is the final physician fallback so an NP or RN working in a
    family-medicine setting is not mislabeled as an MD.
    """
    structured = "\n".join(str(v or "") for v in (profession_type, specialty, headline))
    if isinstance(resume_text, (dict, list)):
        resume_text = json.dumps(resume_text, ensure_ascii=False)
    else:
        resume_text = str(resume_text or "")
    lines = [line.strip() for line in resume_text.splitlines() if line.strip()]
    header = "\n".join(lines[:35])
    credential_lines = "\n".join(line for line in lines if _CREDENTIAL_CONTEXT.search(line))
    full = resume_text or structured

    if _strong(_CRNA, structured, header, credential_lines, full, full_hits=1):
        return Decision("APP", "CRNA", "CRNA / nurse anesthetist")
    if _strong(_NP, structured, header, credential_lines, full, full_hits=1) \
            or _strong(_NP_BARE, structured, header, credential_lines, full, full_hits=2):
        return Decision("APP", "NP", "Nurse Practitioner")

    md_structured = (profession_type or "").strip().upper().strip(".") == "MD"
    if md_structured or _MD_PHRASE.search(full) or _MD_CREDENTIAL.search(header) \
            or _MD_CREDENTIAL.search(credential_lines):
        return Decision("Physicians", "MD", "MD / Doctor of Medicine")

    allied_source = "\n".join((structured, full))
    for profession, pattern in _ALLIED:
        if pattern.search(allied_source):
            return Decision("Allied", profession, profession)

    if _REGISTERED_NURSE.search(full) or _strong(
        _RN, structured, header, credential_lines, full, full_hits=1
    ):
        return Decision("Nursing", "RN", "RN / Registered Nurse")
    if _strong(_LPN, structured, header, credential_lines, full, full_hits=1):
        return Decision("Nursing", "LPN", "LPN / Licensed Practical Nurse")
    if _strong(_CNA, structured, header, credential_lines, full, full_hits=1):
        return Decision("Nursing", "CNA", "CNA / Certified Nursing Assistant")

    if _FAMILY_MEDICINE.search(structured) or _hits(_FAMILY_MEDICINE, full) >= 2:
        return Decision("Physicians", "MD", "Family Medicine")
    # Do not use ``structured`` here. In older data, the stored specialty may
    # itself be the false-positive value we are repairing.
    return _nursing_specialty_fallback(resume_text, header, profession_type)


def _resume_text(resume_url: str) -> str:
    key, is_local = storage.key_from_url(resume_url)
    data = storage.download_bytes(key, prefer_local=is_local)
    return extract_text_from_bytes(data, Path(key).name)


def _classify_one(row: dict) -> tuple[str, Decision | None, str | None]:
    decision = classify_resume_role(
        row.get("resume_sections") or "",
        profession_type=row.get("profession_type"),
        specialty=row.get("specialty"),
        headline=row.get("headline"),
    )
    if decision:
        return row["profile_id"], decision, None
    try:
        full_text = _resume_text(row["resume_url"])
        decision = classify_resume_role(
            full_text,
            profession_type=row.get("profession_type"),
            specialty=row.get("specialty"),
            headline=row.get("headline"),
        )
        return row["profile_id"], decision, None
    except Exception as exc:  # noqa: BLE001 - isolate corrupt/unreadable resumes
        return row["profile_id"], None, type(exc).__name__


def _apply_batch(db, decisions: list[tuple[str, Decision]]) -> int:
    if not decisions:
        return 0
    updated = 0
    statement = sql_text("""
        UPDATE profiles
        SET provider_category = :category,
            profession_type = :profession,
            completion_score = LEAST(
                100,
                completion_score + CASE
                    WHEN profession_type IS NULL OR length(trim(profession_type)) = 0 THEN 10
                    ELSE 0
                END
            ),
            search_text = lower(concat_ws(' ',
                first_name, last_name, headline, bio, specialty,
                CAST(:profession AS VARCHAR), city, state_code, american_board,
                CAST(:category AS VARCHAR)
            )),
            updated_at = now()
        WHERE profile_id = :profile_id
          AND (provider_category = 'Other' OR provider_category IS NULL)
    """)
    for profile_id, decision in decisions:
        result = db.execute(statement, {
            "profile_id": profile_id,
            "category": decision.category,
            "profession": decision.profession,
        })
        updated += result.rowcount or 0
    return updated


def _append_change_log(path: Path, rows_by_id: dict[str, dict],
                       decisions: list[tuple[str, Decision]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    applied_at = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as handle:
        for profile_id, decision in decisions:
            old = rows_by_id[profile_id]
            handle.write(json.dumps({
                "applied_at": applied_at,
                "profile_id": profile_id,
                "old_category": old.get("provider_category"),
                "old_profession": old.get("profession_type"),
                "old_completion_score": old.get("completion_score"),
                "new_category": decision.category,
                "new_profession": decision.profession,
                "evidence": decision.evidence,
            }, sort_keys=True) + "\n")


def _resume_cursor(checkpoint: Path, *, restart: bool) -> str | None:
    if restart:
        return None
    if checkpoint.exists():
        try:
            value = json.loads(checkpoint.read_text(encoding="utf-8")).get("last_profile_id")
            if value:
                return str(value)
        except (OSError, ValueError, TypeError):
            pass
    return None


def _write_checkpoint(path: Path, last_profile_id: str, stats: Counter,
                      *, complete: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_profile_id": last_profile_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "complete": complete,
        "run_stats": dict(stats),
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Reclassify Other profiles from stored resumes")
    parser.add_argument("--apply", action="store_true", help="write changes; default is dry-run")
    parser.add_argument("--limit", type=int, default=0, help="maximum profiles to process")
    parser.add_argument("--offset", type=int, default=0, help="profiles to skip")
    parser.add_argument("--workers", type=int, default=12, help="parallel resume readers")
    parser.add_argument(
        "--executor", choices=("process", "thread"), default="process",
        help="process is faster for PDF parsing; thread is a compatibility fallback",
    )
    parser.add_argument("--batch-size", type=int, default=250, help="download/commit batch size")
    parser.add_argument("--only-listable", action="store_true", help="exclude hidden profiles")
    parser.add_argument(
        "--changes", default="exports/provider_reclassification_changes.jsonl",
        help="append-only audit/rollback log written after each committed batch",
    )
    parser.add_argument(
        "--checkpoint", default="exports/provider_reclassification_checkpoint.json",
        help="resume cursor updated after each committed batch",
    )
    parser.add_argument(
        "--restart", action="store_true",
        help="ignore the checkpoint/change-log cursor and scan from the beginning",
    )
    parser.add_argument(
        "--start-after", default="",
        help="explicit profile_id cursor (takes precedence over checkpoint)",
    )
    args = parser.parse_args()

    stats: Counter[str] = Counter()
    batch_size = max(1, args.batch_size)
    processed_scope = 0
    changes_path = Path(args.changes)
    checkpoint_path = Path(args.checkpoint)
    last_profile_id = args.start_after.strip() or _resume_cursor(
        checkpoint_path, restart=args.restart or not args.apply
    )
    first_page = True
    print(
        f"Apply={args.apply} Workers={args.workers} BatchSize={batch_size} "
        f"Executor={args.executor} ResumeAfter={last_profile_id or '-'}",
        flush=True,
    )
    executor_cls = ProcessPoolExecutor if args.executor == "process" else ThreadPoolExecutor
    with executor_cls(max_workers=max(1, args.workers)) as pool:
        while True:
            page_size = batch_size
            if args.limit:
                page_size = min(page_size, args.limit - processed_scope)
                if page_size <= 0:
                    break
            batch_stmt = select(
                Profile.profile_id, Profile.resume_url, Profile.profession_type,
                Profile.specialty, Profile.headline, Profile.resume_sections,
                Profile.is_listable, Profile.provider_category,
                Profile.completion_score,
            ).where(
                or_(Profile.provider_category == "Other", Profile.provider_category.is_(None)),
                Profile.resume_url.isnot(None),
            )
            if args.only_listable:
                batch_stmt = batch_stmt.where(Profile.is_listable.is_(True))
            if last_profile_id is not None:
                batch_stmt = batch_stmt.where(Profile.profile_id > last_profile_id)
            batch_stmt = batch_stmt.order_by(Profile.profile_id).limit(page_size)
            if first_page and args.offset:
                batch_stmt = batch_stmt.offset(args.offset)
            with SessionLocal() as read_db:
                batch = [dict(row) for row in read_db.execute(batch_stmt).mappings().all()]
            first_page = False
            if not batch:
                break
            batch_last_profile_id = batch[-1]["profile_id"]
            processed_scope += len(batch)
            results = list(pool.map(_classify_one, batch))
            decisions: list[tuple[str, Decision]] = []
            for profile_id, decision, error in results:
                stats["processed"] += 1
                if error:
                    stats["failed"] += 1
                elif decision is None:
                    stats["unmatched"] += 1
                else:
                    stats[f"matched_{decision.category}"] += 1
                    stats[f"profession_{decision.profession}"] += 1
                    decisions.append((profile_id, decision))

            if args.apply:
                with SessionLocal() as write_db:
                    updated = _apply_batch(write_db, decisions)
                    write_db.commit()
                stats["updated"] += updated
                if updated:
                    _append_change_log(
                        changes_path,
                        {row["profile_id"]: row for row in batch},
                        decisions,
                    )
                _write_checkpoint(checkpoint_path, batch_last_profile_id, stats)
            last_profile_id = batch_last_profile_id
            print(
                f"Progress={processed_scope:,} Cursor={last_profile_id} "
                f"matched={sum(v for k, v in stats.items() if k.startswith('matched_')):,} "
                f"unmatched={stats['unmatched']:,} failed={stats['failed']:,}",
                flush=True,
            )

    if args.apply and last_profile_id:
        _write_checkpoint(checkpoint_path, last_profile_id, stats, complete=True)
    print("Summary")
    for key, value in sorted(stats.items()):
        print(f"  {key}={value:,}")
    if not args.apply:
        print("Dry run only; no database rows changed. Add --apply to commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
