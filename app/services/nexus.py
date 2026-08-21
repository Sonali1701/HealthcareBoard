"""LaborEdge Nexus ATS integration — pull open reqs into our job board.

Flow (per the LaborEdge "JOB_BOARD" API spec):
  1. OAuth: POST {token_url} with a fixed Basic client credential + the
     agency's username/password/organizationCode. Returns a bearer
     access_token that expires in ~15 minutes.
  2. GET {base}/api/job-service/v1/ats/external/jobs/search with a JSON filter
     body. Paginated 100 records at a time via pagingDetails.start.

Credentials come from settings (env). Nothing here writes to the database —
map_job() returns plain field dicts that app/sync_nexus_jobs.py upserts.
"""
from __future__ import annotations

import time
from datetime import date
from typing import Iterator, Optional

import httpx

from ..config import settings
from ..models.enums import JobStatus, JobType

JOBS_PATH = "/api/job-service/v1/ats/external/jobs/search"
EXTERNAL_SOURCE = "nexus"


class NexusError(RuntimeError):
    """Raised when Nexus is not configured or an API call fails."""


class NexusClient:
    """A thin, synchronous client with automatic token renewal.

    Nexus tokens live ~899s. Rather than run the refresh-token dance (whose
    token is single-use), we simply re-authenticate a few seconds before
    expiry — robust for a batch sync that may page through many jobs.
    """

    def __init__(self, *, timeout: float = 30.0):
        if not settings.nexus_enabled:
            raise NexusError("Nexus is disabled (set NEXUS_ENABLED=true).")
        missing = [k for k in ("nexus_username", "nexus_password", "nexus_org_code")
                   if not getattr(settings, k)]
        if missing:
            raise NexusError(f"Missing Nexus credentials: {', '.join(missing)}")
        self._client = httpx.Client(timeout=timeout)
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "NexusClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- auth ---------------------------------------------------------------
    def _authenticate(self) -> None:
        res = self._client.post(
            settings.nexus_token_url,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"basic {settings.nexus_basic_auth}",
            },
            data={
                "username": settings.nexus_username,
                "password": settings.nexus_password,
                "grant_type": settings.nexus_grant_type,
                "organizationCode": settings.nexus_org_code,
            },
        )
        if res.status_code != 200:
            raise NexusError(f"Nexus auth failed ({res.status_code}): {res.text[:300]}")
        data = res.json()
        token = data.get("access_token")
        if not token:
            raise NexusError("Nexus auth response had no access_token")
        self._token = token
        # Renew 60s early; default to a conservative 600s if not reported.
        self._expires_at = time.monotonic() + int(data.get("expires_in", 600)) - 60

    def _bearer(self) -> str:
        if not self._token or time.monotonic() >= self._expires_at:
            self._authenticate()
        return self._token  # type: ignore[return-value]

    # -- jobs ---------------------------------------------------------------
    def search_jobs(self, filters: dict, start: int = 0) -> dict:
        body = {**filters, "pagingDetails": {"start": start}}
        # The doc labels this GET-with-body, but the live endpoint answers 405 to
        # GET and expects POST (as its sibling candidate "search" does).
        def _call():
            return self._client.post(
                settings.nexus_base_url.rstrip("/") + JOBS_PATH,
                headers={"Authorization": f"Bearer {self._bearer()}",
                         "Content-Type": "application/json"},
                json=body,
            )
        res = _call()
        if res.status_code == 401:  # token rotated mid-run — re-auth once
            self._authenticate()
            res = _call()
        if res.status_code != 200:
            raise NexusError(f"Nexus jobs search failed ({res.status_code}): {res.text[:300]}")
        return res.json()

    def iter_open_jobs(self, extra_filters: Optional[dict] = None,
                       page_size: int = 100) -> Iterator[dict]:
        """Yield every OPEN job record, transparently paginating."""
        filters = {"jobStatusCode": "OPEN", **(extra_filters or {})}
        start = 0
        while True:
            payload = self.search_jobs(filters, start=start)
            records = payload.get("records") or []
            for rec in records:
                yield rec
            count = payload.get("count", 0)
            start += page_size
            if start >= count or not records:
                break


# --- mapping -----------------------------------------------------------------

_JOB_TYPE_MAP = {
    "travel": JobType.travel,
    "perm": JobType.staff,
    "permanent": JobType.staff,
    "staff": JobType.staff,
    "perdiem": JobType.per_diem,
    "per diem": JobType.per_diem,
    "per_diem": JobType.per_diem,
    "local": JobType.contract,
    "contract": JobType.contract,
}


def _money(value) -> Optional[float]:
    """Parse a number or a "$1,695.37" string into a float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _money_range(value) -> tuple[Optional[float], Optional[float]]:
    """Parse "$32.38 - $42.38" into (min, max)."""
    if not value:
        return (None, None)
    parts = [p for p in str(value).split("-") if p.strip()]
    nums = [_money(p) for p in parts]
    nums = [n for n in nums if n is not None]
    if not nums:
        return (None, None)
    return (min(nums), max(nums))


def _date(value) -> Optional[date]:
    """Parse a "YYYY-MM-DD" (or ISO datetime) string into a date."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _shift_type(rec: dict) -> Optional[str]:
    """Bucket a start time into days/evenings/nights."""
    start = rec.get("shiftStartTime1")
    if not start:
        return None
    try:
        hour = int(str(start).split(":")[0])
    except (ValueError, IndexError):
        return None
    if 5 <= hour < 12:
        return "days"
    if 12 <= hour < 17:
        return "evenings"
    return "nights"


def map_job(rec: dict) -> Optional[dict]:
    """Map one Nexus job record to JobPosting field kwargs.

    Returns None for records that should not be listed (hidden from the
    external board, or without a usable id).
    """
    ext_id = rec.get("id")
    if ext_id is None:
        return None
    if rec.get("displayOnExternalJobBoard") is False:
        return None

    profession = (rec.get("profession") or "").strip()
    specialty = (rec.get("specialty") or "").strip()
    title = rec.get("jobTitle") or " – ".join([p for p in (profession, specialty) if p]) \
        or rec.get("title") or "Healthcare position"

    hourly_min, hourly_max = _money_range(rec.get("hourlyPayRange"))
    if hourly_min is None:
        hourly_min = hourly_max = _money(rec.get("hourlyPay"))

    job_type = _JOB_TYPE_MAP.get((rec.get("jobType") or "").strip().lower(), JobType.travel)

    certs = []
    for key in ("requiredCertificationsForOnboarding", "requiredCertificationsForSubmittal"):
        for c in (rec.get(key) or []):
            if c and c not in certs:
                certs.append(c)

    lodging = _money(rec.get("lodgingAmount"))
    benefits = ["housing"] if lodging else []

    urgency = (rec.get("positionUrgencyId") or "").strip().upper()
    is_urgent = bool(rec.get("asap")) or urgency in {"URGENT", "HIGH"}

    status = JobStatus.active if (rec.get("jobStatusCode") == "OPEN") else JobStatus.closed

    start_date = _date(rec.get("startDate"))

    requirements = {
        "weekly_pay": _money(rec.get("weeklyPay")),
        "weekly_pay_range": rec.get("weeklyPayRange"),
        "hours_per_week": rec.get("scheduledHrs1"),
        "shift": rec.get("shift"),
        "duration_weeks": rec.get("duration") or rec.get("length"),
        "openings": rec.get("noOfOpenings"),
        "lodging_amount": lodging,
        "mie_amount": _money(rec.get("mealAmount")),
        "hourly_stipend": _money(rec.get("hourlyStipendRate")),
        "vms": rec.get("vms"),
        "offering": rec.get("offering"),
    }
    # Drop empty keys to keep the JSON tidy.
    requirements = {k: v for k, v in requirements.items() if v not in (None, "")}

    return {
        "external_source": EXTERNAL_SOURCE,
        "external_id": str(ext_id),
        "title": title[:300],
        "specialty": specialty[:100] or None,
        "profession_type": profession[:50] or None,
        "job_type": job_type,
        "shift_type": _shift_type(rec),
        "pay_rate_min": hourly_min,
        "pay_rate_max": hourly_max,
        "pay_unit": "hourly",
        "housing_stipend": lodging,
        "signing_bonus": _money(rec.get("signOnBonus")),
        "city": (rec.get("clientCity") or "")[:120] or None,
        "state_code": (rec.get("clientStateCode") or "")[:2] or None,
        "facility": (rec.get("clientName") or "")[:200] or None,
        "agency": (rec.get("vms") or rec.get("clientPrimaryDivision") or "")[:150] or None,
        "req_code": (str(rec.get("postingId") or rec.get("externalJobPostingId") or ext_id))[:60],
        "description": rec.get("description") or None,
        "requirements": requirements,
        "benefits": benefits,
        "required_certifications": certs,
        "status": status,
        "is_urgent": is_urgent,
        "is_featured": bool(rec.get("featuredJob")),
        "start_date": start_date,
    }
