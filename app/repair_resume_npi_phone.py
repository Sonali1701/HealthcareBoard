"""Remove NPI values mis-extracted into recovered résumé phone fields.

Dry-run by default. Use ``python -m app.repair_resume_npi_phone --apply`` to
replace phone == NPI values only when the résumé has a separate phone number
on a line explicitly labeled Phone/Cell/Mobile/Telephone; otherwise clear the
invalid phone value. The NPI field and résumé text remain untouched.
"""
from __future__ import annotations

import argparse
import re

from sqlalchemy import text

from .database import engine

PHONE_RE = re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
NPI_RE = re.compile(r"\bNPI[:#\s]*([0-9]{10})\b", re.IGNORECASE)
PHONE_LABEL_RE = re.compile(r"\b(phone|mobile|cell|telephone|tel\.?)\b", re.IGNORECASE)


def _labelled_alternate_phone(resume_text: str) -> str | None:
    """Return a separate phone candidate only when its own line labels it."""
    scrubbed = NPI_RE.sub("", resume_text or "")
    for match in PHONE_RE.finditer(scrubbed):
        line_start = scrubbed.rfind("\n", 0, match.start()) + 1
        line_end = scrubbed.find("\n", match.end())
        if line_end < 0:
            line_end = len(scrubbed)
        if PHONE_LABEL_RE.search(scrubbed[line_start:line_end]):
            return match.group(0)[:30]
    return None


def repair(*, apply: bool = False) -> dict[str, int]:
    statement = text("""select provider_id, phone, extracted_text
      from public."New_provider"
      where review_status <> 'rejected' and phone is not null
        and npi_number is not null
        and regexp_replace(phone, '[^0-9]', '', 'g') = npi_number""")
    updates: list[dict[str, object]] = []
    with engine.connect().execution_options(stream_results=True) as conn:
        for row in conn.execute(statement).mappings():
            updates.append({
                "provider_id": row["provider_id"],
                "old_phone": row["phone"],
                "new_phone": _labelled_alternate_phone(row["extracted_text"]),
            })

    if not apply:
        return {
            "suspected_npi_as_phone": len(updates),
            "restorable_labeled_phone": sum(u["new_phone"] is not None for u in updates),
            "would_clear_phone": sum(u["new_phone"] is None for u in updates),
            "updated": 0,
        }

    changed = 0
    update_sql = text("""update public."New_provider"
      set phone = :new_phone, updated_at = now()
      where provider_id = :provider_id and phone = :old_phone""")
    with engine.begin() as conn:
        for start in range(0, len(updates), 2000):
            batch = updates[start:start + 2000]
            result = conn.execute(update_sql, batch)
            changed += result.rowcount
    return {
        "suspected_npi_as_phone": len(updates),
        "restored_from_explicit_phone_label": sum(u["new_phone"] is not None for u in updates),
        "cleared_as_unverified": sum(u["new_phone"] is None for u in updates),
        "updated": changed,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the reviewed corrections")
    args = parser.parse_args()
    print(repair(apply=args.apply))
