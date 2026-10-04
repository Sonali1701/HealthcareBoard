"""Ceipal applicant duplicate checks and creation for Medhunt enrichment."""
from __future__ import annotations

import json
import re
import threading
import time
from urllib.parse import urljoin, urlparse

import httpx

from ..config import settings

_LOCK = threading.RLock()
_APPLICANT_CACHE: tuple[float, list[dict]] | None = None
_COUNTRY_CACHE: tuple[float, list[dict]] | None = None
_STATE_CACHE: tuple[float, list[dict]] | None = None
_TOKEN_CACHE: tuple[float, str] | None = None
# CEIPAL v1's documented applicant-list endpoint paginates the whole tenant and
# has no email/phone filter. Keep a short tenant snapshot to stay within its
# rate limit while limiting the stale window for applicants added elsewhere.
_CACHE_SECONDS = 30
_MAX_PAGES = 200


class MedhuntCeipalError(RuntimeError):
    pass


def _configured() -> bool:
    return bool(
        settings.ceipal_enabled and settings.ceipal_email
        and settings.ceipal_password and settings.ceipal_api_key
    )


def _api_url(path: str) -> str:
    return f"{settings.ceipal_base_url.rstrip('/')}/v1/{path.lstrip('/')}"


def _token(client: httpx.Client) -> str:
    global _TOKEN_CACHE
    if _TOKEN_CACHE and _TOKEN_CACHE[0] > time.time():
        return _TOKEN_CACHE[1]
    if not _configured():
        raise MedhuntCeipalError("Ceipal applicant integration is not configured.")
    response = client.post(
        _api_url("createAuthtoken"),
        json={
            "email": settings.ceipal_email,
            "password": settings.ceipal_password,
            "api_key": settings.ceipal_api_key,
        },
    )
    if response.is_error:
        raise MedhuntCeipalError("Ceipal authentication failed.")
    try:
        body = response.json()
    except ValueError as exc:
        raise MedhuntCeipalError("Ceipal authentication returned invalid JSON.") from exc
    for source in (body, body.get("data") if isinstance(body, dict) else None):
        if isinstance(source, dict):
            token = source.get("access_token") or source.get("token")
            if token:
                _TOKEN_CACHE = (time.time() + 55 * 60, str(token))
                return str(token)
    raise MedhuntCeipalError("Ceipal authentication returned no access token.")


def _records(body) -> tuple[list[dict], dict]:
    if isinstance(body, list):
        return [dict(row) for row in body if isinstance(row, dict)], {}
    if not isinstance(body, dict):
        raise MedhuntCeipalError("Ceipal applicant search returned an invalid response.")
    for key in ("results", "result", "records", "items", "applicants"):
        value = body.get(key)
        if isinstance(value, list):
            return [dict(row) for row in value if isinstance(row, dict)], body
    data = body.get("data")
    if isinstance(data, list):
        return [dict(row) for row in data if isinstance(row, dict)], body
    if isinstance(data, dict):
        return _records(data)
    if any(key in body for key in ("applicant_id", "firstname", "email")):
        return [body], body
    return [], body


def _applicant_snapshot(client: httpx.Client, token: str) -> list[dict]:
    global _APPLICANT_CACHE
    now = time.time()
    with _LOCK:
        if _APPLICANT_CACHE and _APPLICANT_CACHE[0] > now:
            return [dict(row) for row in _APPLICANT_CACHE[1]]
        url = _api_url("getApplicantsList")
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
                    if urlparse(candidate_url).netloc.casefold() != urlparse(url).netloc.casefold():
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
            try:
                total_count = int(metadata.get("count") or metadata.get("total_count") or 0)
            except (TypeError, ValueError):
                total_count = 0
            if total_count and len(all_rows) < total_count:
                page += 1
                next_url = url
                continue
            break
        else:
            raise MedhuntCeipalError("Ceipal applicant listing exceeded the page limit.")
        _APPLICANT_CACHE = (time.time() + _CACHE_SECONDS, all_rows)
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
    phones = {_phone_key(value) for value in _values(candidate.get("phones"))}
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


def _location_ids(client: httpx.Client, token: str, state: str) -> tuple[str, str]:
    """Resolve US country/state IDs required by the v1 applicant form."""
    global _COUNTRY_CACHE, _STATE_CACHE
    now = time.time()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if not _COUNTRY_CACHE or _COUNTRY_CACHE[0] <= now:
        response = client.get(_api_url("getCountriesList"), headers=headers)
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
            _api_url("getStatesList"), headers=headers, params={"country": "US"},
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


def _create_applicant(client: httpx.Client, token: str, candidate: dict) -> str:
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
    country_id, state_id = _location_ids(client, token, state) if state else ("", "")
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
    files = {key: (None, _form_value(value)) for key, value in fields.items() if value}
    response = client.post(
        _api_url("createApplicant"),
        headers={"Authorization": f"Bearer {token}"},
        files=files,
    )
    if response.is_error:
        raise MedhuntCeipalError("Ceipal applicant creation failed.")
    try:
        body = response.json()
    except ValueError:
        body = {}
    if isinstance(body, dict):
        status = str(body.get("status") or "").casefold()
        if status in {"error", "failed", "failure"} or body.get("success") is False:
            raise MedhuntCeipalError("Ceipal applicant creation was rejected.")
        return str(body.get("applicant_id") or body.get("id") or "")
    return ""


def upload_candidate(candidate: dict) -> dict:
    """Upload the assigned candidate directly, without querying Ceipal."""
    if not _configured():
        raise MedhuntCeipalError("Ceipal applicant integration is not configured.")
    with httpx.Client(timeout=45.0) as client, _LOCK:
        token = _token(client)
        applicant_id = _create_applicant(client, token, candidate)
        return {
            "state": "uploaded_to_ceipal",
            "blocked": False,
            "checked": False,
            "applicant_id": applicant_id,
        }


def check_and_create(candidate: dict) -> dict:
    """Backward-compatible route name; semantics are direct upload only."""
    return upload_candidate(candidate)
