"""Correct source-confirmed midwives incorrectly marked MD.

The affected profiles already have the Nursing category and Certified Nurse
Midwife specialty. Stored source text must explicitly state that role and must
not mention a physician. Run dry by default; use ``--apply`` to make audited,
resumable changes in Neon.
"""
from __future__ import annotations

import argparse

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from .config import settings


EXPECTED_HOST = "ep-nameless-flower-ah7umr3s-pooler.c-3.us-east-1.aws.neon.tech"
PREDICATE = """
    p.specialty = 'Certified Nurse Midwife'
    AND p.job_category = 'Nursing'
    AND p.profession_type = 'MD'
    AND lower(coalesce(p.resume_sections->>'raw_extracted_text', ''))
        LIKE '%certified nurse midwife%'
    AND lower(coalesce(p.resume_sections->>'raw_extracted_text', ''))
        NOT LIKE '%physician%'
"""
NEW_HEADLINE = """CASE WHEN p.headline IN
    ('Practice Address', 'Certified Nurse Midwife MD', 'MD')
    THEN 'Certified Nurse Midwife' ELSE p.headline END"""
SELECT_IDS = text(f"SELECT p.profile_id FROM profiles p WHERE {PREDICATE} LIMIT :take")
INSERT_AUDIT = text(f"""
INSERT INTO profile_specialty_category_repairs_20261024
(profile_id, old_specialty, old_category, old_profession, old_headline,
 old_is_listable, old_screen_reason, new_category, new_profession,
 new_headline, new_is_listable, new_screen_reason, evidence)
SELECT p.profile_id, p.specialty, p.job_category, p.profession_type,
       p.headline, p.is_listable, p.screen_reason, p.job_category,
       'CNM', {NEW_HEADLINE}, p.is_listable, p.screen_reason,
       'source_explicit_CNM_no_physician'
FROM profiles p WHERE p.profile_id IN :ids AND {PREDICATE}
ON CONFLICT (profile_id) DO NOTHING RETURNING profile_id
""").bindparams(bindparam("ids", expanding=True))
UPDATE = text(f"""
UPDATE profiles p SET profession_type = 'CNM', headline = {NEW_HEADLINE},
    search_text = lower(concat_ws(' ', p.first_name, p.last_name,
        {NEW_HEADLINE}, p.bio, p.specialty, 'CNM', p.city, p.state_code,
        p.american_board, p.provider_category, p.job_category)),
    updated_at = now()
WHERE p.profile_id IN :ids AND {PREDICATE}
RETURNING p.profile_id
""").bindparams(bindparam("ids", expanding=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 500:
        parser.error("batch size must be 1..500")
    url = make_url(settings.database_url)
    if url.host != EXPECTED_HOST or url.database != "neondb":
        raise RuntimeError("Unexpected database target; repair refused")

    engine = create_engine(settings.database_url, pool_pre_ping=True,
                           connect_args={"connect_timeout": 8})
    total = 0
    take = args.batch_size
    while True:
        try:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL statement_timeout = '90s'"))
                ids = [row[0] for row in conn.execute(SELECT_IDS, {"take": take})]
                if not ids:
                    break
                if not args.apply:
                    print(f"At least {len(ids)} source-confirmed CNM profiles need repair")
                    break
                logged = [row[0] for row in conn.execute(INSERT_AUDIT,
                                                        {"ids": ids})]
                if not logged:
                    raise RuntimeError("No audit progress")
                changed = [row[0] for row in conn.execute(UPDATE,
                                                         {"ids": logged})]
                if len(changed) != len(logged):
                    raise RuntimeError("Audit/update mismatch")
                total += len(changed)
        except OperationalError as exc:
            if "statement timeout" not in str(exc).lower() or take <= 10:
                raise
            take = max(10, take // 2)
            print(f"Timed out; retrying batches of {take}", flush=True)
            continue
        print(f"Committed {total} CNM corrections", flush=True)
    if args.apply:
        print(f"Complete: {total} corrected", flush=True)


if __name__ == "__main__":
    main()
