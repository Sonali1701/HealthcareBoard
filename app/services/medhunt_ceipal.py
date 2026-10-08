"""Ceipal applicant duplicate checks and creation for Medhunt enrichment."""
from __future__ import annotations

import json
import hashlib
import re
import threading
import time
from xml.etree import ElementTree
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import text

from ..config import settings

_LOCK = threading.RLock()
_COUNTRY_CACHE: tuple[float, list[dict]] | None = None
_STATE_CACHE: tuple[float, list[dict]] | None = None
_TOKEN_CACHE: dict[str, tuple[float, str]] = {}
# This tenant honors an exact primary-email filter. Phone filters are ignored.
# An incomplete or ignored filter must never authorize a new applicant.
_CACHE_SECONDS = 30
_MAX_PAGES = 200


class MedhuntCeipalError(RuntimeError):
    pass


def _configuration(configuration: dict | None = None) -> dict:
    if configuration:
        return dict(configuration)
    return {
        "base_url": settings.ceipal_base_url,
        "email": settings.ceipal_email,
        "password": settings.ceipal_password,
        "api_key": settings.ceipal_api_key,
        "enabled": settings.ceipal_enabled,
    }


def _configured(configuration: dict | None = None) -> bool:
    values = _configuration(configuration)
    return bool(
        values.get("enabled", True) and values.get("email")
        and values.get("password") and values.get("api_key")
    )


def _api_url(path: str, configuration: dict | None = None) -> str:
    base_url = str(_configuration(configuration).get("base_url") or "https://api.ceipal.com")
    endpoint = path.lstrip("/")
    if endpoint.split("/", 1)[0] in {"getApplicantsList", "getCountriesList", "getStatesList"}:
        endpoint = endpoint.rstrip("/") + "/"
    return f"{base_url.rstrip('/')}/v1/{endpoint}"


def _token(client: httpx.Client, configuration: dict | None = None) -> str:
    values = _configuration(configuration)
    cache_key = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    cached = _TOKEN_CACHE.get(cache_key)
    if cached and cached[0] > time.time():
        return cached[1]
    if not _configured(values):
        raise MedhuntCeipalError("Ceipal applicant integration is not configured.")
    response = client.post(
        _api_url("createAuthtoken", values),
        json={
            "email": values["email"],
            "password": values["password"],
            "api_key": values["api_key"],
        },
    )
    if response.is_error:
        raise MedhuntCeipalError("Ceipal authentication failed.")
    try:
        body = response.json()
    except ValueError:
        try:
            root = ElementTree.fromstring(response.content)
            body = {element.tag: element.text for element in root.iter() if element.text}
        except ElementTree.ParseError as exc:
            raise MedhuntCeipalError("Ceipal authentication returned an invalid response.") from exc
    for source in (body, body.get("data") if isinstance(body, dict) else None):
        if isinstance(source, dict):
            token = source.get("access_token") or source.get("token")
            if token:
                _TOKEN_CACHE[cache_key] = (time.time() + 55 * 60, str(token))
                return str(token)
    raise MedhuntCeipalError("Ceipal authentication returned no access token.")


def _records(body) -> tuple[list[dict], dict]:
    if isinstance(body, list):
        return [dict(row) for row in body if isinstance(row, dict)], {}
    if not isinstance(body, dict):
        raise MedhuntCeipalError("Ceipal applicant search returned an invalid response.")
    if body.get("success") is False or body.get("error") or str(body.get("status") or "").casefold() in {
        "0", "400", "403", "429", "error", "failed", "failure",
    }:
        raise MedhuntCeipalError("Ceipal applicant search was rejected.")
    for key in ("results", "result", "records", "items", "applicants"):
        value = body.get(key)
        if isinstance(value, list):
            return [dict(row) for row in value if isinstance(row, dict)], body
    data = body.get("data")
    if isinstance(data, list):
        return [dict(row) for row in data if isinstance(row, dict)], body
    if isinstance(data, dict):
        rows, nested_metadata = _records(data)
        return rows, {**body, **nested_metadata}
    if any(key in body for key in ("applicant_id", "firstname", "email")):
        return [body], body
    return [], body


def _applicant_snapshot(client: httpx.Client, token: str,
                        configuration: dict | None = None) -> list[dict]:
    # Read afresh while the caller holds the per-contact database lock. A
    # process-local cache can miss an applicant created by another web worker.
    with _LOCK:
        url = _api_url("getApplicantsList", configuration)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        all_rows: list[dict] = []
        seen_ids: set[str] = set()
        seen_signatures: set[str] = set()
        page = 1
        next_url = url
        while page <= _MAX_PAGES:
            params = {"page": page} if next_url == url and page > 1 else None
            response = client.get(next_url, headers=headers, params=params)
            if response.is_error:
                raise MedhuntCeipalError("Ceipal applicant search failed.")
            try:
                rows, metadata = _records(response.json())
            except ValueError as exc:
                raise MedhuntCeipalError("Ceipal applicant search returned invalid JSON.") from exc
            if not rows:
                try:
                    declared_total = int(metadata.get("count") or metadata.get("total_count") or 0)
                except (TypeError, ValueError):
                    declared_total = 0
                if declared_total and len(all_rows) < declared_total:
                    raise MedhuntCeipalError("Ceipal applicant listing ended before all records were read.")
                break
            page_ids = [str(row.get("id") or row.get("applicant_id") or "") for row in rows]
            signature = "|".join(page_ids) if any(page_ids) else json.dumps(rows, sort_keys=True, default=str)
            if signature and signature in seen_signatures:
                # The endpoint ignored the page query. We cannot certify that
                # an absent contact is really absent, so fail closed.
                raise MedhuntCeipalError("Ceipal applicant pagination did not advance.")
            seen_signatures.add(signature)
            for row in rows:
                row_id = str(row.get("id") or row.get("applicant_id") or "")
                if row_id and row_id in seen_ids:
                    continue
                if row_id:
                    seen_ids.add(row_id)
                all_rows.append(row)
            next_value = metadata.get("next") or metadata.get("next_page")
            if next_value:
                next_text = str(next_value).strip()
                if next_text.isdigit():
                    next_url = url
                    page = int(next_text)
                else:
                    candidate_url = urljoin(url, next_text)
                    if (
                        urlparse(candidate_url).scheme != "https"
                        or urlparse(candidate_url).netloc.casefold() != urlparse(url).netloc.casefold()
                    ):
                        raise MedhuntCeipalError("Ceipal returned an unsafe pagination link.")
                    next_url = candidate_url
                    page += 1
                continue
            try:
                num_pages = int(metadata.get("num_pages") or metadata.get("total_pages") or 0)
            except (TypeError, ValueError):
                num_pages = 0
            if num_pages and page < num_pages:
                page += 1
                next_url = url
                continue
            if num_pages:
                break
            try:
                total_count = int(metadata.get("count") or metadata.get("total_count") or 0)
            except (TypeError, ValueError):
                total_count = 0
            if total_count and len(all_rows) < total_count:
                page += 1
                next_url = url
                continue
            # Some CEIPAL tenants omit pagination metadata. Ask for the next
            # page rather than mistaking the first page for the entire tenant.
            page += 1
            next_url = url
        else:
            raise MedhuntCeipalError("Ceipal applicant listing exceeded the page limit.")
        return [dict(row) for row in all_rows]


def _values(value) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = [value]
    output = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("value") or item.get("email") or item.get("number") or ""
        text = " ".join(str(item or "").split())
        if text and text not in output:
            output.append(text)
    return output


def _email_key(value: str) -> str:
    value = value.strip().casefold()
    return value if "@" in value else ""


def _phone_key(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else ""


def _candidate_matches(candidate: dict, applicants: list[dict]) -> dict | None:
    emails = {_email_key(value) for value in _values(candidate.get("emails"))}
    emails.discard("")
    phones = {
        _phone_key(value)
        for value in _values(candidate.get("phones")) + _values(candidate.get("wireless_phones"))
    }
    phones.discard("")
    email_fields = ("email", "email_address_1", "email_address_2", "alternate_email")
    phone_fields = (
        "mobile_number", "other_phone", "home_phone_number", "work_phone_number",
        "phone", "alternative_phone", "alternate_phone",
    )
    matches = []
    for applicant in applicants:
        found_by = set()
        for field in email_fields:
            if any(_email_key(value) in emails for value in _values(applicant.get(field))):
                found_by.add("email")
        for field in phone_fields:
            if any(_phone_key(value) in phones for value in _values(applicant.get(field))):
                found_by.add("phone")
        if found_by:
            matches.append((applicant, found_by))
    if not matches:
        return None
    applicant, matched_by = matches[0]
    return {
        "state": "already_in_ceipal",
        "blocked": True,
        "checked": True,
        "applicant_id": str(applicant.get("applicant_id") or applicant.get("id") or ""),
        "status": str(applicant.get("applicant_status") or ""),
        "matched_by": sorted(matched_by),
        "match_count": len(matches),
    }


def acquire_duplicate_locks(db, candidate: dict, configuration: dict | None = None) -> None:
    """Serialize same-contact upload attempts across Halo web workers on Postgres."""
    bind = db.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", "") != "postgresql":
        return
    values = _configuration(configuration)
    tenant = hashlib.sha256(json.dumps([
        values.get("base_url"), values.get("email"), values.get("api_key"),
    ], sort_keys=True).encode()).hexdigest()
    contacts = {
        *(('email', key) for key in (_email_key(item) for item in _values(candidate.get("emails"))) if key),
        *(('phone', key) for key in (
            _phone_key(item) for item in (
                _values(candidate.get("phones")) + _values(candidate.get("wireless_phones"))
            )
        ) if key),
    }
    lock_ids = sorted({
        int.from_bytes(hashlib.sha256(f"{tenant}:{kind}:{value}".encode()).digest()[:8], "big", signed=True)
        for kind, value in contacts
    })
    for lock_id in lock_ids:
        db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})


def _find_existing_applicant(client: httpx.Client, token: str, candidate: dict,
                             configuration: dict | None = None) -> dict | None:
    """Use CEIPAL's exact primary-email filter; never scan a partial tenant."""
    emails = sorted({_email_key(value) for value in _values(candidate.get("emails"))} - {""})
    if not emails:
        raise MedhuntCeipalError(
            "Ceipal cannot safely check duplicates for a candidate without an email address."
        )
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = _api_url("getApplicantsList", configuration)
    for email in emails:
        response = client.get(url, headers=headers, params={"email": email})
        if response.is_error or response.is_redirect:
            raise MedhuntCeipalError("Ceipal applicant search failed.")
        try:
            rows, metadata = _records(response.json())
            count = int(metadata.get("count", len(rows)))
        except (ValueError, TypeError) as exc:
            raise MedhuntCeipalError("Ceipal applicant search returned an invalid response.") from exc
        if count > len(rows):
            raise MedhuntCeipalError("Ceipal email search returned an incomplete result.")
        # CEIPAL silently ignores unsupported filters. Require every returned
        # row to match the queried email before trusting a zero or a match.
        if any(_email_key(row.get("email") or "") != email for row in rows):
            raise MedhuntCeipalError("Ceipal did not apply the email filter.")
        match = _candidate_matches(candidate, rows)
        if match:
            return match
    return None


def _location_ids(client: httpx.Client, token: str, state: str,
                  configuration: dict | None = None) -> tuple[str, str]:
    """Resolve US country/state IDs required by the v1 applicant form."""
    global _COUNTRY_CACHE, _STATE_CACHE
    now = time.time()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if not _COUNTRY_CACHE or _COUNTRY_CACHE[0] <= now:
        response = client.get(_api_url("getCountriesList", configuration), headers=headers)
        if response.is_error:
            return "", ""
        try:
            body = response.json()
        except ValueError:
            return "", ""
        rows, _ = _records(body)
        _COUNTRY_CACHE = (time.time() + _CACHE_SECONDS, rows)
    countries = _COUNTRY_CACHE[1]
    country = next((row for row in countries if str(row.get("iso") or "").casefold() == "us"), None)
    if not country:
        country = next((row for row in countries if "united states" in str(row.get("name") or "").casefold()), None)
    if not country:
        return "", ""
    country_id = str(country.get("id") or "")
    if not _STATE_CACHE or _STATE_CACHE[0] <= now:
        response = client.get(
            _api_url("getStatesList", configuration), headers=headers, params={"country": "US"},
        )
        if response.is_error:
            return country_id, ""
        try:
            body = response.json()
        except ValueError:
            return country_id, ""
        rows, _ = _records(body)
        _STATE_CACHE = (time.time() + _CACHE_SECONDS, rows)
    normalized = state.strip().casefold()
    row = next((item for item in _STATE_CACHE[1] if normalized in {
        str(item.get("name") or "").strip().casefold(),
        str(item.get("abbreviation") or "").strip().casefold(),
    }), None)
    return country_id, str((row or {}).get("id") or "")


def _form_value(value: str) -> str:
    # Ceipal's v1 custom form expects JSON scalar text with a trailing comma.
    return json.dumps(str(value), ensure_ascii=False) + ","


def _create_applicant(client: httpx.Client, token: str, candidate: dict,
                      configuration: dict | None = None) -> str:
    name_parts = " ".join(str(candidate.get("name") or "").split()).split()
    if not name_parts:
        raise MedhuntCeipalError("Candidate name is required by Ceipal.")
    email = next((value for value in _values(candidate.get("emails")) if _email_key(value)), "")
    phone = next((value for value in _values(candidate.get("wireless_phones")) if _phone_key(value)), "")
    if not phone:
        phone = next((value for value in _values(candidate.get("phones")) if _phone_key(value)), "")
    location = str(candidate.get("location") or "").split(",", 1)
    city = location[0].strip() if location else ""
    state = location[1].strip() if len(location) > 1 else ""
    country_id, state_id = (
        _location_ids(client, token, state, configuration) if state else ("", "")
    )
    fields = {
        "standard_fields.firstname": name_parts[0],
        "standard_fields.lastname": name_parts[-1] if len(name_parts) > 1 else "",
        "standard_fields.email": email,
        "standard_fields.mobile_number": phone,
        "standard_fields.city": city,
    }
    if country_id:
        fields["standard_fields.country"] = country_id
    if state_id:
        fields["standard_fields.state"] = state_id
    if not email:
        raise MedhuntCeipalError("Ceipal applicant creation requires an email address.")
    # CEIPAL v1 accepts ordinary form fields. Quoted JSON scalars with trailing
    # commas (as in the documentation's generated curl example) are rejected
    # as an invalid email by the live API.
    form = {key: value for key, value in fields.items() if value}
    response = client.post(
        _api_url("createApplicant", configuration),
        headers={"Authorization": f"Bearer {token}"},
        data=form,
    )
    if response.is_error:
        raise MedhuntCeipalError("Ceipal applicant creation failed.")
    try:
        body = response.json()
    except ValueError:
        body = {}
    if isinstance(body, dict):
        status = str(body.get("status") or "").casefold()
        if status in {"0", "400", "403", "429", "error", "failed", "failure"} or body.get("success") is False:
            raise MedhuntCeipalError("Ceipal applicant creation was rejected.")
        nested = body.get("data") if isinstance(body.get("data"), dict) else {}
        applicant_id = str(
            body.get("applicant_id") or body.get("id")
            or nested.get("applicant_id") or nested.get("id") or ""
        )
        if applicant_id:
            return applicant_id
        if response.status_code == 201 and str(body.get("success")) == "1":
            # The live endpoint acknowledges creation without an applicant ID.
            # Read by exact email to confirm the write and obtain the ID.
            existing = _find_existing_applicant(client, token, {"emails": [email]}, configuration)
            if existing and existing.get("applicant_id"):
                return str(existing["applicant_id"])
    # The write may have succeeded despite an unrecognizable response. The
    # Medhunt outbox will mark it indeterminate and will not create again.
    raise MedhuntCeipalError("Ceipal applicant creation outcome is unconfirmed.")


def upload_candidate(candidate: dict, configuration: dict | None = None) -> dict:
    """Check exact primary emails, then create only when no match exists."""
    if not _configured(configuration):
        raise MedhuntCeipalError("Ceipal applicant integration is not configured.")
    with httpx.Client(timeout=45.0) as client, _LOCK:
        token = _token(client, configuration)
        existing = _find_existing_applicant(client, token, candidate, configuration)
        if existing:
            return existing
        applicant_id = _create_applicant(client, token, candidate, configuration)
        return {
            "state": "uploaded_to_ceipal",
            "blocked": False,
            "checked": True,
            "applicant_id": applicant_id,
        }


def check_and_create(candidate: dict, configuration: dict | None = None) -> dict:
    """Backward-compatible name for duplicate-checked upload."""
    return upload_candidate(candidate, configuration)
