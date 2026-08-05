"""
Batch enrichment orchestration: enrich stored candidates via Enformion, apply the
do-not-contact list, and advance the pipeline stage. Deterministic + testable.
"""
from __future__ import annotations

from . import store, enformion_client as ef, verification


def enrich_candidate(cid: int) -> dict:
    cand = store.get_candidate(cid)
    if not cand:
        return {"error": "candidate not found"}
    res = ef.enrich(cand["name"], cand.get("location", ""))
    status = res.get("status", "error")

    # enforce do-not-contact: drop any suppressed emails/phones
    emails = [e for e in res.get("emails", []) if not store.is_dnc(e)]
    phones = [p for p in res.get("phones", []) if not store.is_dnc(p)]
    res = {**res, "emails": emails, "phones": phones}
    verification_result = verification.assess(cand, res)
    email_checks = {item["value"]: item for item in verification_result["emails"]}
    phone_checks = {item["value"]: item for item in verification_result["phones"]}
    emails = [
        value for value in emails
        if email_checks.get(value, {}).get("format_valid")
        and email_checks.get(value, {}).get("deliverability") not in ("invalid", "disposable")
    ]
    phones = [
        value for value in phones
        if phone_checks.get(value, {}).get("format_valid")
        and phone_checks.get(value, {}).get("line_status") != "invalid"
    ]
    if status == "success" and (
        verification_result["identity_status"] != "provider_match"
        or not (emails or phones)
    ):
        status = "no_match"
        emails = []
        phones = []

    fields = {
        "phones": phones, "emails": emails,
        "addresses": res.get("addresses", []),
        "enrich_status": status,
        "confidence": verification_result["identity_confidence"],
        "verification": verification_result,
    }
    # advance stage only on a real contactable match
    if status == "success" and (emails or phones) and cand["stage"] == "new":
        fields["stage"] = "enriched"
    store.update_candidate(cid, **fields)
    return {"id": cid, **fields, "error": res.get("error")}


def enrich_batch(job_id: int | None = None, only_pending: bool = True) -> dict:
    cands = store.list_candidates(job_id=job_id)
    done, matched, errors = 0, 0, 0
    for c in cands:
        if only_pending and c["enrich_status"] == "success":
            continue
        r = enrich_candidate(c["id"])
        done += 1
        if r.get("enrich_status") == "success" and (r.get("emails") or r.get("phones")):
            matched += 1
        elif r.get("error"):
            errors += 1
    return {"processed": done, "matched": matched, "errors": errors,
            "total_candidates": len(cands)}


def _unique(values: list[str], existing: list[str] | None = None) -> list[str]:
    output = []
    seen = set()
    for value in [*(existing or []), *(values or [])]:
        cleaned = str(value or "").strip()
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        output.append(cleaned)
        if len(output) >= 20:
            break
    return output


def save_provider_result(cid: int, result: dict) -> dict:
    """Validate and store a user-triggered browser-directory result."""
    cand = store.get_candidate(cid)
    if not cand:
        return {"error": "candidate not found"}
    source = str(result.get("source") or "").strip().lower()
    if source != "usphonebook":
        return {"error": "unsupported browser lookup provider"}

    emails = _unique([
        value for value in result.get("emails", [])
        if not store.is_dnc(str(value))
    ])
    phones = _unique([
        value for value in result.get("phones", [])
        if not store.is_dnc(str(value))
    ])
    addresses = _unique(result.get("addresses", []))
    provider_result = {
        "source": source,
        "status": "success" if result.get("status") == "success" else "no_match",
        "matched_name": str(result.get("matched_name") or "")[:200],
        "emails": emails,
        "phones": phones,
        "addresses": addresses,
        "confidence": max(0.0, min(1.0, float(result.get("confidence") or 0))),
    }
    verification_result = verification.assess(cand, provider_result)
    email_checks = {item["value"]: item for item in verification_result["emails"]}
    phone_checks = {item["value"]: item for item in verification_result["phones"]}
    emails = [
        value for value in emails
        if email_checks.get(value, {}).get("format_valid")
        and email_checks.get(value, {}).get("deliverability") not in ("invalid", "disposable")
    ]
    phones = [
        value for value in phones
        if phone_checks.get(value, {}).get("format_valid")
        and phone_checks.get(value, {}).get("line_status") != "invalid"
    ]
    if (
        provider_result["status"] != "success"
        or verification_result["identity_status"] != "provider_match"
        or not (emails or phones)
    ):
        return {
            "id": cid,
            "enrich_status": "no_match",
            "emails": [],
            "phones": [],
            "addresses": [],
            "confidence": verification_result["identity_confidence"],
            "verification": verification_result,
            "error": "USPhoneBook identity/contact evidence was not strong enough to save.",
        }

    merged_emails = _unique(emails, cand.get("emails", []))
    merged_phones = _unique(phones, cand.get("phones", []))
    merged_addresses = _unique(addresses, cand.get("addresses", []))
    verification_result.update({
        "source": source,
        "profile_url": str(result.get("profile_url") or "")[:2000],
        "provider_contacts": {
            "emails": emails,
            "phones": phones,
            "addresses": addresses,
        },
    })
    fields = {
        "emails": merged_emails,
        "phones": merged_phones,
        "addresses": merged_addresses,
        "enrich_status": "success",
        "confidence": verification_result["identity_confidence"],
        "verification": verification_result,
    }
    if cand["stage"] == "new":
        fields["stage"] = "enriched"
    store.update_candidate(cid, **fields)
    return {
        "id": cid,
        **fields,
        "emails": emails,
        "phones": phones,
        "addresses": addresses,
        "stored_emails": merged_emails,
        "stored_phones": merged_phones,
        "stored_addresses": merged_addresses,
        "error": None,
    }
