"""Repair three verified NPI role/category mappings in small audited batches.

Run with ``python -m app.repair_npi_taxonomy --apply``. A dry run is the default.
The source titles correspond to NUCC's Registered Nurse and Pharmacist roles.
"""
from __future__ import annotations

import argparse

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from .config import settings


EXPECTED_HOST = "ep-nameless-flower-ah7umr3s-pooler.c-3.us-east-1.aws.neon.tech"
OLD_CATEGORY = "Allied Health & Technicians"
MAPPINGS = (
    ("Nutrition Support Pharmacist", "Pharmacy"),
    ("Nutrition Support Registered Nurse", "Nursing"),
    ("Lactation Consultant (Registered Nurse)", "Nursing"),
)

CREATE_AUDIT = text("""
CREATE TABLE IF NOT EXISTS profile_specialty_category_repairs_20261024 (
    profile_id varchar(36) PRIMARY KEY,
    old_specialty text, old_category text, old_profession text,
    old_headline text, old_is_listable boolean, old_screen_reason text,
    new_category text, new_profession text, new_headline text,
    new_is_listable boolean, new_screen_reason text,
    evidence text NOT NULL, repaired_at timestamptz NOT NULL DEFAULT now()
)
""")
SELECT_IDS = text("""
SELECT profile_id FROM profiles
WHERE specialty = :specialty AND job_category = :old_category
  AND capture_source = 'NPI' AND npi_number IS NOT NULL
  AND resume_url IS NULL
LIMIT :batch_size
""")
INSERT_AUDIT = text("""
INSERT INTO profile_specialty_category_repairs_20261024
(profile_id, old_specialty, old_category, old_profession, old_headline,
 old_is_listable, old_screen_reason, new_category, new_profession,
 new_headline, new_is_listable, new_screen_reason, evidence)
SELECT p.profile_id, p.specialty, p.job_category, p.profession_type,
       p.headline, p.is_listable, p.screen_reason, :new_category,
       p.profession_type, p.headline, p.is_listable, p.screen_reason,
       'NUCC_NPI_exact_role'
FROM profiles p
WHERE p.profile_id IN :ids AND p.specialty = :specialty
  AND p.job_category = :old_category AND p.capture_source = 'NPI'
  AND p.npi_number IS NOT NULL AND p.resume_url IS NULL
ON CONFLICT (profile_id) DO NOTHING
RETURNING profile_id
""").bindparams(bindparam("ids", expanding=True))
UPDATE = text("""
UPDATE profiles p
SET job_category = CAST(:new_category AS varchar),
    search_text = lower(concat_ws(' ', p.first_name, p.last_name,
        p.headline, p.bio, p.specialty, p.profession_type, p.city,
        p.state_code, p.american_board, p.provider_category,
        CAST(:new_category AS varchar))),
    updated_at = now()
WHERE p.profile_id IN :ids AND p.specialty = :specialty
  AND p.job_category = :old_category AND p.capture_source = 'NPI'
  AND p.npi_number IS NOT NULL AND p.resume_url IS NULL
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
    if args.apply:
        with engine.begin() as conn:
            conn.execute(CREATE_AUDIT)

    for specialty, new_category in MAPPINGS:
        total = 0
        batch_size = args.batch_size
        while True:
            stage = "starting"
            try:
                with engine.begin() as conn:
                    conn.execute(text("SET LOCAL statement_timeout = '90s'"))
                    params = {"specialty": specialty, "old_category": OLD_CATEGORY,
                              "new_category": new_category, "batch_size": batch_size}
                    stage = "select"
                    ids = [row[0] for row in conn.execute(SELECT_IDS, params)]
                    if not ids:
                        break
                    if not args.apply:
                        print(f"{specialty}: at least {len(ids)} rows need repair")
                        break
                    params["ids"] = ids
                    stage = "audit"
                    logged = [row[0] for row in conn.execute(INSERT_AUDIT, params)]
                    if not logged:
                        raise RuntimeError(f"No audit progress for {specialty}")
                    params["ids"] = logged
                    stage = "update"
                    changed = [row[0] for row in conn.execute(UPDATE, params)]
                    if len(changed) != len(logged):
                        raise RuntimeError(f"Audit/update mismatch for {specialty}")
                    total += len(changed)
            except OperationalError as exc:
                if "statement timeout" not in str(exc).lower() or batch_size <= 10:
                    raise
                batch_size = max(10, batch_size // 2)
                print(f"{specialty}: {stage} timed out; retrying {batch_size}", flush=True)
                continue
            print(f"{specialty}: committed {total}", flush=True)
        if args.apply:
            print(f"{specialty}: complete, {total} changed", flush=True)


if __name__ == "__main__":
    main()
