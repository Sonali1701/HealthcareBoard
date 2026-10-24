"""Correct source-confirmed pharmacists incorrectly marked as MD.

The first 160 source characters must identify a pharmacist, and the full stored
text must not mention a physician. Unclear records stay for manual review.
"""
from __future__ import annotations

import argparse

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import make_url

from .config import settings


EXPECTED_HOST = "ep-nameless-flower-ah7umr3s-pooler.c-3.us-east-1.aws.neon.tech"
PREDICATE = """
    p.job_category = 'Pharmacy' AND p.profession_type = 'MD'
    AND p.specialty IN ('Clinical Pharmacist', 'Pharmacist')
    AND lower(left(coalesce(p.resume_sections->>'raw_extracted_text', ''), 160))
        LIKE '%pharmacist%'
    AND lower(coalesce(p.resume_sections->>'raw_extracted_text', ''))
        NOT LIKE '%physician%'
"""
NEW_HEADLINE = "CASE WHEN p.headline = 'MD' THEN 'Pharmacist' ELSE p.headline END"
CREATE_AUDIT = text("""
CREATE TABLE IF NOT EXISTS profile_pharmacist_profession_repairs_20261024 (
    profile_id varchar(36) PRIMARY KEY,
    old_profession text NOT NULL,
    old_headline text,
    new_profession text NOT NULL,
    new_headline text,
    evidence text NOT NULL,
    repaired_at timestamptz NOT NULL DEFAULT now()
)
""")
SELECT_IDS = text(f"""
SELECT p.profile_id FROM profiles p WHERE {PREDICATE} LIMIT :take
""")
INSERT_AUDIT = text(f"""
INSERT INTO profile_pharmacist_profession_repairs_20261024
(profile_id, old_profession, old_headline, new_profession, new_headline, evidence)
SELECT p.profile_id, p.profession_type, p.headline, 'Pharmacist',
       {NEW_HEADLINE}, 'source_header_pharmacist_no_physician'
FROM profiles p WHERE p.profile_id IN :ids AND {PREDICATE}
ON CONFLICT (profile_id) DO NOTHING RETURNING profile_id
""").bindparams(bindparam("ids", expanding=True))
UPDATE = text(f"""
UPDATE profiles p SET profession_type = 'Pharmacist', headline = {NEW_HEADLINE},
    search_text = lower(concat_ws(' ', p.first_name, p.last_name,
        {NEW_HEADLINE}, p.bio, p.specialty, 'Pharmacist', p.city, p.state_code,
        p.american_board, p.provider_category, p.job_category)),
    updated_at = now()
WHERE p.profile_id IN :ids AND {PREDICATE}
RETURNING p.profile_id
""").bindparams(bindparam("ids", expanding=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=200)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 500:
        parser.error("batch size must be 1..500")
    url = make_url(settings.database_url)
    if url.host != EXPECTED_HOST or url.database != "neondb":
        raise RuntimeError("Unexpected database target; repair refused")
    engine = create_engine(settings.database_url, pool_pre_ping=True,
                           connect_args={"connect_timeout": 8})
    if args.apply:
        with engine.begin() as conn:
            conn.execute(CREATE_AUDIT)
    total = 0
    while True:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '120s'"))
            ids = [row[0] for row in conn.execute(SELECT_IDS, {"take": args.batch_size})]
            if not ids:
                break
            if not args.apply:
                print(f"At least {len(ids)} source-confirmed pharmacist profiles need repair")
                break
            logged = [row[0] for row in conn.execute(INSERT_AUDIT, {"ids": ids})]
            if len(logged) != len(ids):
                raise RuntimeError("Audit progress stalled")
            changed = [row[0] for row in conn.execute(UPDATE, {"ids": logged})]
            if len(changed) != len(logged):
                raise RuntimeError("Audit/update mismatch")
            total += len(changed)
        print(f"Corrected {total} pharmacist professions", flush=True)
    if args.apply:
        print(f"Complete: {total} corrected", flush=True)


if __name__ == "__main__":
    main()
