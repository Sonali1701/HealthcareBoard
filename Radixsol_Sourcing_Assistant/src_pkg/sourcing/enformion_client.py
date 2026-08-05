"""
Enformion / Endato contact-enrichment client.

Takes a candidate name (+ optional location) and returns phone/email/address.
Includes a deterministic DEMO mode so the whole product runs before you wire the
real API key. Never scrapes anything — this is a licensed HTTPS data call.
"""
from __future__ import annotations

import hashlib
import re

import httpx

from . import config


def _clean_phone(raw: str) -> str:
    d = re.sub(r"\D", "", str(raw or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return f"({d[0:3]}) {d[3:6]}-{d[6:10]}" if len(d) == 10 else str(raw or "").strip()


def _dedupe(seq):
    seen, out = set(), []
    for s in seq:
        if not s:
            continue
        k = re.sub(r"\D", "", s) if any(c.isdigit() for c in s) else s.lower()
        if k and k not in seen:
            seen.add(k); out.append(s)
    return out


def _split_name(full: str):
    parts = (full or "").strip().split()
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return parts[0], " ".join(parts[1:-1]), parts[-1]


def _demo_enrich(name: str, location: str) -> dict:
    """Deterministic fake data keyed off the name so demos are stable."""
    h = int(hashlib.sha256((name + location).encode()).hexdigest(), 16)
    area = 200 + (h % 700)
    mid = 200 + (h // 7 % 700)
    last = h % 10000
    handle = re.sub(r"[^a-z]", ".", name.lower()).strip(".")
    return {
        "status": "success",
        "matched_name": name,
        "phones": [_clean_phone(f"{area}{mid:03d}{last:04d}"[:10])],
        "emails": [f"{handle}@example.com"],
        "addresses": [f"{100 + h % 9900} Main St, {location or 'Atlanta, GA'}"],
        "confidence": round(0.6 + (h % 40) / 100, 2),
        "source": "enformion (demo)",
    }


def enrich(name: str, location: str = "") -> dict:
    """Return {status, matched_name, phones[], emails[], addresses[], confidence, source, error?}."""
    name = (name or "").strip()
    if not name:
        return {"status": "error", "error": "Missing name", "phones": [], "emails": [], "addresses": []}

    if config.DEMO_MODE:
        return _demo_enrich(name, location)

    first, middle, last = _split_name(name)
    body = {"FirstName": first, "MiddleName": middle, "LastName": last,
            "Dob": "", "Age": 0, "Phone": "", "Email": ""}
    if location:
        body["Address"] = {"addressLine1": "", "addressLine2": location}
    headers = {
        "galaxy-ap-name": config.ENFORMION_AP_NAME,
        "galaxy-ap-password": config.ENFORMION_AP_PASSWORD,
        "galaxy-search-type": config.ENFORMION_SEARCH_TYPE,
        "galaxy-client-type": "Python",
        "Content-Type": "application/json", "Accept": "application/json",
    }
    try:
        with httpx.Client(timeout=config.HTTP_TIMEOUT) as c:
            r = c.post(config.ENFORMION_URL, json=body, headers=headers)
        if r.status_code in (401, 403):
            return {"status": "error", "error": f"Enformion auth failed ({r.status_code})",
                    "phones": [], "emails": [], "addresses": []}
        if r.status_code >= 400:
            return {"status": "error", "error": f"Enformion HTTP {r.status_code}: {r.text[:160]}",
                    "phones": [], "emails": [], "addresses": []}
        return _map(r.json(), name)
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"{type(e).__name__}: {e}",
                "phones": [], "emails": [], "addresses": []}


def _map(data: dict, name: str) -> dict:
    person = data.get("person") or data.get("Person") or data or {}
    phones, emails, addrs = [], [], []
    for p in (person.get("phones") or person.get("Phones") or person.get("phoneNumbers") or []):
        num = p.get("number") or p.get("phoneNumber") or p.get("Number") if isinstance(p, dict) else p
        if num:
            phones.append(_clean_phone(num))
    for e in (person.get("emails") or person.get("Emails") or person.get("emailAddresses") or []):
        v = e.get("email") or e.get("emailAddress") or e.get("Email") if isinstance(e, dict) else e
        if v:
            emails.append(str(v).strip())
    for a in (person.get("addresses") or person.get("Addresses") or []):
        if isinstance(a, dict):
            line = re.sub(r"\s+", " ", " ".join(str(a.get(k, "")) for k in
                          ("addressLine1", "addressLine2", "fullAddress", "FullAddress")).strip())
            if line:
                addrs.append(line)
        elif a:
            addrs.append(str(a).strip())
    phones, emails, addrs = _dedupe(phones), _dedupe(emails), _dedupe(addrs)
    return {
        "status": "success" if (phones or emails) else "no_match",
        "matched_name": (person.get("name") or person.get("fullName") or name or "").strip() or name,
        "phones": phones, "emails": emails, "addresses": addrs,
        "confidence": 1.0 if (phones or emails) else 0.0,
        "source": "enformion",
    }
