"""Audit and publish recoverable hidden résumé profiles.

Only hidden ``resume_parse`` profiles with a structurally valid first/last name
and a résumé URL enter the audit. A profile is published only when its stored
résumé can be read, contains meaningful text, and does not duplicate a visible
résumé/email or a same-name phone record. Visibility failures remain untouched.
Every candidate receives a PII-free JSONL audit record.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, text as sql_text
from sqlalchemy.exc import OperationalError

from .database import SessionLocal, engine
from .finalize_provider_categories import _classify_one, _strict_name
from .models import Profile
from .models.enums import ProfileSource


def _normalise_email(value: str | None) -> str:
    return str(value or "").strip().lower()


def _normalise_phone(value: str | None) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else digits


def _name_key(first: str | None, last: str | None) -> str:
    return " ".join(
        re.sub(r"[^a-z]", "", str(value or "").lower())
        for value in (first, last)
    ).strip()


def _resume_key(value: str | None) -> str:
    return str(value or "").strip()


def _fetch(stmt, *, retries: int = 5) -> list[dict]:
    for attempt in range(retries):
        try:
            with SessionLocal() as db:
                return [dict(row) for row in db.execute(stmt).mappings()]
        except OperationalError:
            engine.dispose()
            if attempt == retries - 1:
                raise
            time.sleep(min(2 ** attempt, 8))
    return []


_PUBLISH = sql_text("""
    UPDATE profiles
    SET provider_category = :category,
        profession_type = :profession,
        is_listable = TRUE,
        completion_score = LEAST(
            100,
            completion_score + CASE
                WHEN profession_type IS NULL OR length(trim(profession_type)) = 0
                THEN 10 ELSE 0
            END
        ),
        search_text = lower(concat_ws(' ',
            first_name, last_name, headline, bio, specialty,
            CAST(:profession AS VARCHAR), city, state_code, american_board,
            CAST(:category AS VARCHAR)
        )),
        updated_at = now()
    WHERE profile_id = :profile_id
      AND is_listable IS FALSE
""")


def _apply(rows: list[dict], *, batch_size: int = 250, retries: int = 5) -> int:
    updated = 0
    for start in range(0, len(rows), max(1, batch_size)):
        batch = rows[start:start + max(1, batch_size)]
        for attempt in range(retries):
            db = SessionLocal()
            try:
                for row in batch:
                    updated += db.execute(_PUBLISH, row).rowcount or 0
                db.commit()
                break
            except OperationalError:
                db.rollback()
                engine.dispose()
                if attempt == retries - 1:
                    raise
                time.sleep(min(2 ** attempt, 8))
            finally:
                db.close()
    return updated


def _visible_identity_sets() -> tuple[set[str], set[str], set[tuple[str, str]]]:
    rows = _fetch(select(
        Profile.resume_url, Profile.email, Profile.phone,
        Profile.first_name, Profile.last_name,
    ).where(Profile.is_listable.is_(True)))
    urls: set[str] = set()
    emails: set[str] = set()
    phones_and_names: set[tuple[str, str]] = set()
    for row in rows:
        if key := _resume_key(row.get("resume_url")):
            urls.add(key)
        if key := _normalise_email(row.get("email")):
            emails.add(key)
        phone = _normalise_phone(row.get("phone"))
        name = _name_key(row.get("first_name"), row.get("last_name"))
        if phone and name:
            phones_and_names.add((phone, name))
    return urls, emails, phones_and_names


def _write_manifest(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover valid hidden profiles")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--executor", choices=("process", "thread"), default="process")
    parser.add_argument("--manifest", default="exports/hidden_recovery_audit.jsonl")
    args = parser.parse_args()

    inputs = _fetch(select(
        Profile.profile_id, Profile.first_name, Profile.last_name,
        Profile.resume_url, Profile.email, Profile.phone,
        Profile.profession_type, Profile.specialty, Profile.headline,
        Profile.resume_sections, Profile.provider_category,
        Profile.is_listable, Profile.completion_score, Profile.created_at,
    ).where(
        Profile.is_listable.is_(False),
        Profile.source == ProfileSource.resume_parse,
    ))
    candidates = [
        row for row in inputs
        if _strict_name(row.get("first_name"), row.get("last_name"))
        and _resume_key(row.get("resume_url"))
    ]
    candidates.sort(key=lambda row: (
        -(row.get("completion_score") or 0),
        str(row.get("created_at") or ""),
        row["profile_id"],
    ))
    if args.limit:
        candidates = candidates[:args.limit]

    visible_urls, visible_emails, visible_phone_names = _visible_identity_sets()
    executor_cls = ProcessPoolExecutor if args.executor == "process" else ThreadPoolExecutor
    print(
        f"Candidates={len(candidates):,} Apply={args.apply} "
        f"Workers={args.workers} Executor={args.executor}",
        flush=True,
    )
    with executor_cls(max_workers=max(1, args.workers)) as pool:
        outcomes = list(pool.map(_classify_one, candidates))

    by_id = {row["profile_id"]: row for row in candidates}
    outcome_by_id = {outcome.profile_id: outcome for outcome in outcomes}
    seen_urls = set(visible_urls)
    seen_emails = set(visible_emails)
    seen_phone_names = set(visible_phone_names)
    records: list[dict] = []
    publish: list[dict] = []
    stats: Counter[str] = Counter()
    audited_at = datetime.now(timezone.utc).isoformat()

    # Candidates are already ordered best-first, so duplicate lower-quality rows
    # remain hidden when two hidden profiles represent the same person/document.
    for row in candidates:
        outcome = outcome_by_id[row["profile_id"]]
        status = "publish"
        reason = "Passed recovery gate"
        if not outcome.name_valid:
            status, reason = "keep_hidden", "InvalidName"
        elif not outcome.document_valid:
            status, reason = "keep_hidden", outcome.error or "InvalidDocument"

        url = _resume_key(row.get("resume_url"))
        email = _normalise_email(row.get("email"))
        phone_name = (
            _normalise_phone(row.get("phone")),
            _name_key(row.get("first_name"), row.get("last_name")),
        )
        if status == "publish" and url and url in seen_urls:
            status, reason = "keep_hidden", "DuplicateVisibleOrSelectedResume"
        elif status == "publish" and email and email in seen_emails:
            status, reason = "keep_hidden", "DuplicateVisibleOrSelectedEmail"
        elif status == "publish" and all(phone_name) and phone_name in seen_phone_names:
            status, reason = "keep_hidden", "DuplicateVisibleOrSelectedPhoneAndName"

        decision = outcome.decision
        category = decision.category if decision else "Others"
        profession = decision.profession if decision else row.get("profession_type")
        evidence = decision.evidence if decision else "No approved taxonomy match; Others"
        if status == "publish":
            if url:
                seen_urls.add(url)
            if email:
                seen_emails.add(email)
            if all(phone_name):
                seen_phone_names.add(phone_name)
            publish.append({
                "profile_id": row["profile_id"],
                "category": category,
                "profession": profession,
            })

        stats[status] += 1
        stats[f"reason_{reason}"] += 1
        if status == "publish":
            stats[f"category_{category}"] += 1
        records.append({
            "audited_at": audited_at,
            "profile_id": row["profile_id"],
            "status": status,
            "reason": reason,
            "old_category": row.get("provider_category"),
            "new_category": category if status == "publish" else row.get("provider_category"),
            "old_profession": row.get("profession_type"),
            "new_profession": profession if status == "publish" else row.get("profession_type"),
            "evidence": evidence,
            "read_error": outcome.error,
            "text_chars": outcome.text_chars,
            "name_valid": outcome.name_valid,
            "document_valid": outcome.document_valid,
        })

    _write_manifest(Path(args.manifest), records)
    updated = _apply(publish, batch_size=args.batch_size) if args.apply else 0
    print("Summary")
    for key, value in sorted(stats.items()):
        print(f"  {key}={value:,}")
    print(f"  database_updated={updated:,}")
    if not args.apply:
        print("Dry run only; no database rows changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
