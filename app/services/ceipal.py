"""Ceipal ATS integration — authenticate and pull the Custom Report (jobs).

Flow (per Ceipal docs):
  1. POST {email, password, api_key} to the auth URL -> bearer token.
  2. GET the report URL with `Authorization: Bearer <token>` -> JSON rows.
"""
from __future__ import annotations

import httpx

from ..config import settings


class CeipalError(Exception):
    pass


def get_token() -> str:
    if not (settings.ceipal_email and settings.ceipal_password and settings.ceipal_api_key):
        raise CeipalError("Set CEIPAL_EMAIL, CEIPAL_PASSWORD and CEIPAL_API_KEY in .env first.")
    body = {
        "email": settings.ceipal_email,
        "password": settings.ceipal_password,
        "apiKey": settings.ceipal_api_key,
    }
    with httpx.Client(timeout=30) as client:
        r = client.post(settings.ceipal_auth_url, json=body)
        if r.status_code >= 400:
            raise CeipalError(f"Auth failed ({r.status_code}): {r.text[:300]}")
        data = r.json()
    token = (data.get("access_token") or data.get("token")
             or data.get("authtoken") or data.get("id_token"))
    if not token:
        raise CeipalError(f"No token in auth response. Response keys: {list(data)[:10]}")
    return token


def fetch_report(token: str, url: str | None = None):
    """Return the parsed JSON of one report page."""
    target = url or settings.ceipal_report_url
    if not target:
        raise CeipalError("CEIPAL_REPORT_URL is not set.")
    with httpx.Client(timeout=90, follow_redirects=True) as client:
        r = client.get(target, headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 400:
            raise CeipalError(f"Report fetch failed ({r.status_code}): {r.text[:300]}")
        return r.json()


def _rows(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        v = payload.get("result")
        if isinstance(v, list):
            return v
    return []


def fetch_all_records(token: str, url: str | None = None, max_pages: int = 200) -> list:
    """Follow the report's pagination (`&page=N`) and return every row.

    The API's own `next_page` link points at a broken mobile endpoint, so we
    page the working `get-report-data` URL directly until has_next_page clears.
    """
    base = (url or settings.ceipal_report_url or "").strip()
    if not base:
        raise CeipalError("CEIPAL_REPORT_URL is not set.")
    sep = "&" if "?" in base else "?"
    out: list = []
    page = 1
    while page <= max_pages:
        payload = fetch_report(token, f"{base}{sep}page={page}")
        rows = _rows(payload)
        out.extend(rows)
        if not rows or not (isinstance(payload, dict) and payload.get("has_next_page")):
            break
        page += 1
    return out
