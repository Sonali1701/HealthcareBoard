"""Evidence-based verification for licensed contact-enrichment results.

The enrichment provider supplies possible contact data. This module keeps
identity matching separate from email/phone deliverability so the product does
not describe a plausible contact as "verified" without the relevant evidence.
"""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher

from . import config, llm, contact_validation

_EMAIL_RE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)


def _normal(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _name_score(expected: str, matched: str) -> float:
    expected_normal = _normal(expected)
    matched_normal = _normal(matched)
    if not matched_normal:
        return 0.75  # provider did not return a comparable name
    if expected_normal == matched_normal:
        return 1.0
    expected_tokens = set(expected_normal.split())
    matched_tokens = set(matched_normal.split())
    token_score = (
        len(expected_tokens & matched_tokens) / len(expected_tokens | matched_tokens)
        if expected_tokens and matched_tokens
        else 0.0
    )
    sequence_score = SequenceMatcher(None, expected_normal, matched_normal).ratio()
    return round(max(token_score, sequence_score), 3)


def _location_score(location: str, addresses: list[str]) -> float:
    expected = set(_normal(location).split())
    if not expected:
        return 0.5
    haystack = set(_normal(" ".join(addresses or [])).split())
    if not haystack:
        return 0.5
    return round(len(expected & haystack) / len(expected), 3)


def _email_checks(emails: list[str]) -> list[dict]:
    checks = []
    for email in emails:
        check = {
            "value": email,
            "format_valid": bool(_EMAIL_RE.fullmatch(str(email or "").strip())),
            "deliverability": "not_checked",
        }
        if check["format_valid"]:
            check.update(contact_validation.verify_email(email))
        checks.append(check)
    return checks


def _phone_checks(phones: list[str]) -> list[dict]:
    checks = []
    for phone in phones:
        digits = re.sub(r"\D", "", str(phone or ""))
        checks.append({
            "value": phone,
            "format_valid": 10 <= len(digits) <= 15,
            "line_status": "not_checked",
            "identity_owner": "not_checked",
        })
        if checks[-1]["format_valid"]:
            checks[-1].update(contact_validation.verify_phone(phone))
    return checks


def _ai_identity_score(candidate: dict, result: dict) -> tuple[float | None, str]:
    """Optional second opinion; disabled unless explicitly configured."""
    if not config.AI_MATCH_ENABLED or not llm.available():
        return None, ""
    prompt = f"""
You are reviewing whether a licensed contact-data result refers to the same
recruiting candidate. Use only the supplied evidence. Do not infer missing
facts. Return JSON only:
{{"probability": 0.0, "reason": "brief evidence-based explanation"}}

Indeed candidate:
- name: {candidate.get("name", "")}
- location: {candidate.get("location", "")}
- role/profile text: {str(candidate.get("notes", ""))[:1500]}

Licensed-provider result:
- matched name: {result.get("matched_name", "")}
- addresses: {json.dumps(result.get("addresses", [])[:3])}
- provider confidence: {result.get("confidence", 0)}
""".strip()
    raw = llm.generate(prompt, temperature=0.0, max_tokens=180)
    if not raw:
        return None, ""
    try:
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(cleaned)
        probability = max(0.0, min(1.0, float(parsed["probability"])))
        return probability, str(parsed.get("reason", ""))[:500]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None, ""


def assess(candidate: dict, result: dict) -> dict:
    """Return identity confidence plus honest contact-validation evidence."""
    source = _normal(result.get("source", "")).replace(" ", "_")
    public_directory = source == "usphonebook"
    emails = [str(value).strip() for value in result.get("emails", []) if str(value).strip()]
    phones = [str(value).strip() for value in result.get("phones", []) if str(value).strip()]
    name_score = _name_score(candidate.get("name", ""), result.get("matched_name", ""))
    location_score = _location_score(candidate.get("location", ""), result.get("addresses", []))
    try:
        provider_score = max(0.0, min(1.0, float(result.get("confidence", 0.0))))
    except (TypeError, ValueError):
        provider_score = 0.0

    rule_score = (name_score * 0.55) + (location_score * 0.25) + (provider_score * 0.20)
    ai_score, ai_reason = _ai_identity_score(candidate, result)
    identity_score = (rule_score * 0.75) + (ai_score * 0.25) if ai_score is not None else rule_score

    email_checks = _email_checks(emails)
    phone_checks = _phone_checks(phones)
    has_format_valid_contact = any(item["format_valid"] for item in email_checks + phone_checks)
    if not has_format_valid_contact or result.get("status") != "success":
        identity_status = "no_match"
    elif identity_score >= config.IDENTITY_MATCH_THRESHOLD:
        identity_status = "provider_match"
    elif identity_score >= 0.55:
        identity_status = "review"
    else:
        identity_status = "rejected"

    return {
        "identity_status": identity_status,
        "identity_confidence": round(identity_score, 3),
        "method": (
            ("public_directory+rules" if public_directory else "licensed_provider+rules")
            + ("+ai" if ai_score is not None else "")
        ),
        "source": source or "enrichment_provider",
        "evidence": {
            "name_similarity": name_score,
            "location_similarity": location_score,
            "provider_confidence": provider_score,
            "ai_probability": ai_score,
            "ai_reason": ai_reason,
        },
        "emails": email_checks,
        "phones": phone_checks,
        "disclaimer": (
            "Directory/provider match is not proof of ownership. Email deliverability and "
            "phone line/owner status require dedicated verification services."
        ),
    }
