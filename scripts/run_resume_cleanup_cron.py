"""Screen and safely clean Data-tab resumes in bounded, resumable Neon batches.

Designed for a scheduled server job. All progress and audit flags live in Neon;
the container filesystem and the operator's laptop are not used. No rows are
deleted or made visible. Unresolved flags remain in New_provider_resume_screen.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.audit_recovered_data_resumes import reasons  # noqa: E402
from scripts.clean_audited_resume_names_20261120 import VERSION, proposal  # noqa: E402
from scripts.resume_cleanup_common import database_url  # noqa: E402

JOB = "resume_screen_and_name_cleanup_v1"
ZERO = "00000000-0000-0000-0000-000000000000"


def setup(db) -> None:
    with db.cursor() as cur:
        cur.execute('''CREATE TABLE IF NOT EXISTS public."New_provider_resume_cleanup_progress" (
            job_name text PRIMARY KEY, last_provider_id uuid NOT NULL,
            scanned bigint NOT NULL DEFAULT 0, corrected bigint NOT NULL DEFAULT 0,
            flagged bigint NOT NULL DEFAULT 0, completed_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now())''')
        cur.execute('''CREATE TABLE IF NOT EXISTS public."New_provider_resume_screen" (
            provider_id uuid PRIMARY KEY, flags jsonb NOT NULL,
            source_sha256 char(64), screened_at timestamptz NOT NULL DEFAULT now())''')
        cur.execute('''CREATE TABLE IF NOT EXISTS public."New_provider_resume_name_cleanup_audit" (
            provider_id uuid NOT NULL, cleanup_version text NOT NULL,
            previous_name text NOT NULL, corrected_name text NOT NULL,
            previous_credential text, corrected_credential text NOT NULL,
            source_sha256 char(64), changed_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (provider_id, cleanup_version))''')
        cur.execute('''INSERT INTO public."New_provider_resume_cleanup_progress"
            (job_name,last_provider_id) VALUES (%s,%s::uuid)
            ON CONFLICT (job_name) DO NOTHING''', (JOB, ZERO))
    db.commit()


def process_batch(db, batch_size: int) -> dict:
    with db.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '60000ms'")
        cur.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))", (JOB,))
        if not cur.fetchone()[0]:
            db.rollback()
            return {"busy": True}
        cur.execute('''SELECT last_provider_id::text, scanned, corrected, flagged, completed_at
            FROM public."New_provider_resume_cleanup_progress"
            WHERE job_name=%s FOR UPDATE''', (JOB,))
        last, scanned, corrected, flagged, completed_at = cur.fetchone()
        if completed_at:
            db.commit()
            return {"complete": True, "scanned": scanned, "corrected": corrected, "flagged": flagged}
        cur.execute('''SELECT provider_id::text,source_kind,first_name,last_name,credential,
                    specialty,job_category,city,state_code,left(extracted_text,1400),
                    parse_status,review_status,file_sha256
            FROM public."New_provider" AS p WHERE p.provider_id > %s::uuid
            ORDER BY p.provider_id LIMIT %s''', (last, batch_size))
        rows = cur.fetchall()
        if not rows:
            cur.execute('''UPDATE public."New_provider_resume_cleanup_progress"
                SET completed_at=now(),updated_at=now() WHERE job_name=%s''', (JOB,))
            db.commit()
            return {"complete": True, "scanned": scanned, "corrected": corrected, "flagged": flagged}
        screens = []
        changed = 0
        for row in rows:
            (pid, kind, first, last_name, credential, specialty, category, city,
             state, header, parse_status, review_status, sha) = row
            if kind in {"resume", "generated_cv"} and parse_status == "parsed" and review_status != "rejected":
                fixed = proposal(first, last_name, category, header)
                if fixed:
                    surname, role = fixed
                    with db.cursor() as update:
                        update.execute('''UPDATE public."New_provider" SET last_name=%s,credential=%s,
                            quality_flags=CASE WHEN 'resume_header_name_corrected'=ANY(quality_flags)
                                THEN quality_flags ELSE array_append(quality_flags,'resume_header_name_corrected') END,
                            updated_at=now()
                            WHERE provider_id=%s::uuid AND last_name=%s
                              AND file_sha256 IS NOT DISTINCT FROM %s
                              AND parse_status='parsed' AND review_status <> 'rejected'
                            RETURNING provider_id''', (surname, role, pid, last_name, sha))
                        if update.fetchone():
                            update.execute('''INSERT INTO public."New_provider_resume_name_cleanup_audit"
                                (provider_id,cleanup_version,previous_name,corrected_name,
                                 previous_credential,corrected_credential,source_sha256)
                                VALUES (%s::uuid,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                                (pid, VERSION, f"{first} {last_name}", f"{first} {surname}",
                                 credential, role, sha))
                            last_name, credential = surname, role
                            changed += 1
            flags = reasons((pid, kind, first, last_name, credential, specialty, category,
                             city, state, header, parse_status, review_status))
            if flags:
                screens.append({"provider_id": pid, "flags": flags, "sha": sha})
        ids = [row[0] for row in rows]
        cur.execute('''DELETE FROM public."New_provider_resume_screen"
            WHERE provider_id=ANY(%s::uuid[])''', (ids,))
        if screens:
            cur.execute('''INSERT INTO public."New_provider_resume_screen"
                (provider_id,flags,source_sha256)
                SELECT provider_id,flags,sha FROM jsonb_to_recordset(%s::jsonb)
                AS x(provider_id uuid,flags jsonb,sha char(64))''', (json.dumps(screens),))
        cur.execute('''UPDATE public."New_provider_resume_cleanup_progress"
            SET last_provider_id=%s::uuid,scanned=scanned+%s,
                corrected=corrected+%s,flagged=flagged+%s,updated_at=now()
            WHERE job_name=%s''', (rows[-1][0], len(rows), changed, len(screens), JOB))
    db.commit()
    return {"complete": False, "scanned": scanned + len(rows),
            "corrected": corrected + changed, "flagged": flagged + len(screens)}


def main() -> None:
    batch_size = max(1, min(1000, int(os.getenv("RESUME_CLEANUP_BATCH_SIZE", "500"))))
    max_rows = max(batch_size, int(os.getenv("RESUME_CLEANUP_MAX_ROWS", "50000")))
    url = database_url()
    with psycopg.connect(url, connect_timeout=20) as db:
        setup(db)
        processed = 0
        while processed < max_rows:
            result = process_batch(db, min(batch_size, max_rows - processed))
            print(json.dumps(result), flush=True)
            if result.get("complete") or result.get("busy"):
                break
            processed += batch_size


if __name__ == "__main__":
    main()
