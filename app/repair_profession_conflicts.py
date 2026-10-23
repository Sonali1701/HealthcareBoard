"""Correct narrow profession/category conflicts from stored resume source text.

The source record's ``raw_extracted_text`` was spot-checked against the actual
stored resume files. A first explicit role within its opening 80 characters is
accepted only when it agrees with either the current profession or category.
Ambiguous, missing, and two-field-disagree cases remain untouched.

    python -m app.repair_profession_conflicts --limit 5000
    python -m app.repair_profession_conflicts --apply --batch-size 1000
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from .config import settings
from .repair_missing_provider_taxonomy import PATTERNS, ROLES


ROOT = Path(__file__).resolve().parent.parent
CHECKPOINT = ROOT / "exports" / "profession_conflict_repair_checkpoint.json"
START = "00000000-0000-0000-0000-000000000000"
EXPECTED_HOST = "ep-nameless-flower-ah7umr3s-pooler.c-3.us-east-1.aws.neon.tech"
FOCUSED_ROLES = {"MD", "DO", "PA", "RN", "NP", "CRNA", "LPN", "CNA"}

SELECT = text("""
    SELECT profile_id, job_category, profession_type, specialty, resume_sections
      FROM profiles
     WHERE profile_id > :cursor
       AND (
           (job_category = 'Nursing' AND profession_type IN ('MD','DO','PA'))
        OR (job_category = 'Physician' AND profession_type IN ('RN','PA'))
        OR (job_category = 'Physician Assistant' AND profession_type IN ('MD','DO','RN'))
       )
     ORDER BY profile_id LIMIT :batch_size
""")

CREATE_AUDIT = text("""
    CREATE TABLE IF NOT EXISTS profile_profession_conflict_repairs (
        profile_id VARCHAR(36) PRIMARY KEY,
        old_profession VARCHAR(50) NOT NULL,
        old_category VARCHAR(255) NOT NULL,
        new_profession VARCHAR(50) NOT NULL,
        new_category VARCHAR(255) NOT NULL,
        evidence VARCHAR(80) NOT NULL,
        corrected_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
""")

APPLY = text("""
    WITH incoming AS (
        SELECT * FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS x(
            profile_id text, old_profession text, old_category text,
            new_profession text, new_category text, evidence text)
    ), logged AS (
        INSERT INTO profile_profession_conflict_repairs (
            profile_id, old_profession, old_category,
            new_profession, new_category, evidence)
        SELECT p.profile_id, p.profession_type, p.job_category,
               i.new_profession, i.new_category, i.evidence
          FROM incoming i JOIN profiles p ON p.profile_id = i.profile_id
         WHERE p.profession_type = i.old_profession
           AND p.job_category = i.old_category
           AND p.resume_sections IS NOT NULL
        ON CONFLICT (profile_id) DO NOTHING
        RETURNING profile_id
    )
    UPDATE profiles p
       SET profession_type = i.new_profession,
           job_category = i.new_category,
           search_text = lower(concat_ws(' ', p.first_name, p.last_name,
               p.headline, p.bio, p.specialty, i.new_profession, p.city,
               p.state_code, p.american_board, p.provider_category,
               i.new_category)),
           updated_at = now()
      FROM incoming i JOIN logged l ON l.profile_id = i.profile_id
     WHERE p.profile_id = i.profile_id
       AND p.profession_type = i.old_profession
       AND p.job_category = i.old_category
    RETURNING p.profile_id
""")


def decide(row: dict) -> tuple[str, str, str] | None:
    """Return new profession, category, and evidence code if one field is wrong."""
    sections = row.get("resume_sections")
    raw = sections.get("raw_extracted_text") if isinstance(sections, dict) else None
    if not isinstance(raw, str) or len(raw.strip()) < 40:
        return None
    opening = raw[:160]
    hits = [(match.start(), index, role)
            for index, role in enumerate(ROLES)
            if role.profession in FOCUSED_ROLES
            if (match := PATTERNS[role.profession].search(opening))]
    if not hits:
        return None
    position, _, role = min(hits)
    if position > 80:
        return None
    old_profession = row["profession_type"]
    old_category = row["job_category"]
    if role.category == old_category and role.profession != old_profession:
        return role.profession, old_category, f"source_first_{role.profession.lower()}_profession"
    if role.profession == old_profession and role.category != old_category:
        # ICU/ER nursing-unit labels with an MD/DO source role are too ambiguous
        # to move into Physician without independent specialty review.
        if old_category == "Nursing" and role.profession in {"MD", "DO"}:
            return None
        return old_profession, role.category, f"source_first_{role.profession.lower()}_category"
    return None


def _save(cursor: str, stats: Counter, *, complete: bool = False) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHECKPOINT.with_suffix(".tmp")
    tmp.write_text(json.dumps({"cursor": cursor, "stats": dict(stats),
                               "complete": complete,
                               "updated_at": datetime.now(timezone.utc).isoformat()}, indent=2),
                   encoding="utf-8")
    tmp.replace(CHECKPOINT)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 5000:
        parser.error("batch size must be 1..5000")
    url = make_url(settings.database_url)
    if url.host != EXPECTED_HOST or url.database != "neondb":
        raise RuntimeError("Unexpected database target; profession repair refused")
    cursor = START
    stats: Counter = Counter()
    if args.apply and CHECKPOINT.exists() and not args.restart:
        saved = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
        cursor = saved.get("cursor", START)
        stats.update(saved.get("stats", {}))
    engine = create_engine(settings.database_url, pool_pre_ping=True,
                           connect_args={"connect_timeout": 8})
    if args.apply:
        with engine.begin() as conn:
            conn.execute(CREATE_AUDIT)
    seen = 0
    while True:
        take = min(args.batch_size, args.limit-seen) if args.limit else args.batch_size
        with engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(SELECT, {
                "cursor": cursor, "batch_size": take}).mappings()]
        if not rows:
            if args.apply:
                _save(cursor, stats, complete=True)
            break
        changes = []
        for row in rows:
            decision = decide(row)
            if decision:
                profession, category, evidence = decision
                changes.append({"profile_id": row["profile_id"],
                                "old_profession": row["profession_type"],
                                "old_category": row["job_category"],
                                "new_profession": profession,
                                "new_category": category,
                                "evidence": evidence})
                stats[evidence] += 1
            else:
                stats["unresolved"] += 1
        if args.apply and changes:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL statement_timeout = '120s'"))
                applied = {r[0] for r in conn.execute(APPLY, {"rows": json.dumps(changes)})}
            stats["applied"] += len(applied)
            stats["skipped_concurrent_or_already_applied"] += len(changes)-len(applied)
        stats["scanned"] += len(rows)
        seen += len(rows)
        cursor = rows[-1]["profile_id"]
        if args.apply:
            _save(cursor, stats)
        print(json.dumps({"cursor": cursor, "scanned": stats["scanned"],
                          "decisions": stats["scanned"]-stats["unresolved"],
                          "applied": stats["applied"]}), flush=True)
        if args.limit and seen >= args.limit:
            break
    print(json.dumps({"stats": dict(stats), "complete": not rows}, indent=2), flush=True)


if __name__ == "__main__":
    main()
