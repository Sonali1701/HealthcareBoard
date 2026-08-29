"""Quick Sourcer — external contact lookup for a provider we have no contact for.

The directory is full of people we know by name, credential and city but have no
way to reach: a résumé that never carried an email, an import whose phone column
was empty. Quick Sourcer is a separate service (the Hub) that takes a name and a
location, searches the public people-search sites for a match, and returns an
email, a phone and an address.

Two things about it shape this module:

* **It is slow.** Behind the API a real browser opens and visits the source site,
  so a search takes 30-90 seconds. Everything here is async so a lookup parks on
  the socket instead of holding a worker thread, and the read timeout is generous
  enough that a normal search is not cut off half way through.
* **It answers with whichever site replied**, so the detailed `profile` block
  changes shape between calls. We read the `summary` block instead, which the Hub
  normalises to one fixed shape, and fall back to the flat top-level fields.

Nothing is exposed until QUICK_SOURCER_API_KEY is set: with no key `available()`
is False and no request is ever made.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from ..config import settings

logger = logging.getLogger("healthboard.quick_sourcer")

# The four sites the Hub searches. Recorded with each hit, so a recruiter looking
# at a phone number later can see where it actually came from.
KNOWN_SOURCES = ("usphonebook", "familytreenow", "searchpeoplefree", "truepeoplesearch")


class QuickSourcerError(RuntimeError):
    """The lookup could not be made, or the service answered with an error.

    `status` is the HTTP status our API should return for it, so a problem with
    our own key (a 401 from the Hub) never surfaces as the recruiter's fault.
    """

    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass
class ContactMatch:
    """One person as Quick Sourcer found them, flattened to what we store."""

    found: bool = False
    candidate_id: Optional[int] = None
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    source: Optional[str] = None
    # Everything else the match turned up, so the UI can offer alternatives when
    # the first email or phone is not the right one.
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)


def available() -> bool:
    """Whether a lookup can be attempted at all."""
    return settings.quick_sourcer_ready


def is_masked_email(value: str) -> bool:
    """searchpeoplefree returns its own privacy screen — `jo*****1@yahoo.com`.

    That is not an address anyone can write to, and saving it onto a profile
    would look like a real contact we had found. Treated as no email at all.
    """
    return "*" in value


def _clean(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def _summary_list(summary: dict, key: str) -> list[str]:
    """Pull one list out of the normalised `summary` block.

    `phones` holds objects (number/type/carrier); `emails`, `addresses` and
    `names` hold plain strings. Both are flattened to strings here, keeping the
    Hub's order (it puts the primary first) and dropping duplicates.
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in summary.get(key) or []:
        text = item.get("number") if isinstance(item, dict) else item
        if not isinstance(text, str):
            continue
        text = text.strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def parse_match(payload: Any) -> ContactMatch:
    """Turn a /find or /candidates/{id} body into a ContactMatch."""
    if not isinstance(payload, dict) or not payload.get("found"):
        return ContactMatch(found=False)

    summary = payload.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    emails = [e for e in _summary_list(summary, "emails") if not is_masked_email(e)]
    phones = _summary_list(summary, "phones")
    addresses = _summary_list(summary, "addresses")
    names = _summary_list(summary, "names")

    # The flat top-level fields are the Hub's own best pick, so prefer them and
    # let the summary lists fill in whatever came back empty.
    email = _clean(payload.get("email"), 255)
    if email and is_masked_email(email):
        email = None
    if not email and emails:
        email = emails[0][:255]
    phone = _clean(payload.get("phone"), 30) or (phones[0][:30] if phones else None)
    address = (_clean(payload.get("address"), 255)
               or (addresses[0][:255] if addresses else None))

    candidate_id = payload.get("candidate_id")
    if not isinstance(candidate_id, int):
        candidate_id = None

    return ContactMatch(
        found=True,
        candidate_id=candidate_id,
        name=_clean(payload.get("name"), 200),
        email=email,
        phone=phone,
        address=address,
        source=_clean(payload.get("source"), 40),
        emails=emails,
        phones=phones,
        addresses=addresses,
        names=names,
    )


async def _request(method: str, path: str, *, json: dict | None = None) -> Any:
    """One call to the Hub, with its failures translated into QuickSourcerError."""
    if not available():
        raise QuickSourcerError(
            "Contact lookup is not configured on this server.", status=503)

    url = settings.quick_sourcer_base_url.rstrip("/") + path
    headers = {"X-API-Key": settings.quick_sourcer_api_key.strip()}
    # Connect fast, read slowly: an unreachable host should fail in seconds, but a
    # search that has started needs the full window to finish.
    timeout = httpx.Timeout(settings.quick_sourcer_timeout, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.request(method, url, headers=headers, json=json)
    except httpx.TimeoutException as exc:
        raise QuickSourcerError(
            "The contact lookup timed out. These searches take up to a minute and "
            "a half — try again in a moment.", status=504) from exc
    except httpx.HTTPError as exc:
        logger.warning("quick sourcer unreachable: %s", exc)
        raise QuickSourcerError(
            "Could not reach the contact lookup service.", status=502) from exc

    if res.status_code == 404:
        return None
    if res.status_code in (401, 403):
        # Our key, not the recruiter's problem — say so without leaking the key.
        logger.error("quick sourcer rejected our API key (%s)", res.status_code)
        raise QuickSourcerError(
            "Contact lookup is misconfigured on this server.", status=503)
    if res.status_code >= 400:
        detail = ""
        try:
            body = res.json()
            if isinstance(body, dict):
                detail = str(body.get("detail") or "")
        except ValueError:
            pass
        logger.warning("quick sourcer error %s: %s", res.status_code, detail[:200])
        raise QuickSourcerError(
            detail or "The contact lookup service returned an error.", status=502)

    try:
        return res.json()
    except ValueError as exc:
        raise QuickSourcerError(
            "The contact lookup service returned an unreadable response.") from exc


async def find(name: str, location: str | None = None) -> ContactMatch:
    """Search for a person by name, plus a location to disambiguate a common one.

    Takes 30-90 seconds. A miss is not proof there is nothing out there — the
    source site may simply have blocked that search — so callers should present
    it as "nothing this time", never as "no contact exists".
    """
    name = (name or "").strip()
    if not name:
        raise QuickSourcerError("A name is needed to search.", status=400)

    body: dict[str, str] = {"name": name}
    location = (location or "").strip()
    if location:
        body["location"] = location
    return parse_match(await _request("POST", "/find", json=body))


async def fetch_candidate(candidate_id: int) -> ContactMatch:
    """Re-read a person the Hub already found. Instant — nothing is searched."""
    payload = await _request("GET", f"/candidates/{int(candidate_id)}")
    if payload is None:                       # 404 — the Hub no longer has this id
        return ContactMatch(found=False)
    return parse_match(payload)
