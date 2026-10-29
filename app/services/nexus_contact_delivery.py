"""Fast, durable, create-only delivery of revealed contacts to Nexus."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import timedelta
from typing import Any, Mapping

import httpx
from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError

from ..database import SessionLocal, utcnow
from ..models import AtsOrganizationIntegration, NexusContactDelivery, Profile, User
from . import ats_connections, storage

logger = logging.getLogger("healthboard.nexus_contact_delivery")


class NexusWriteUncertain(RuntimeError):
    """A candidate create failed and cannot be retried without duplicate risk."""


def _snapshot(profile: Profile) -> dict:
    npi = (profile.npi_number or "").strip()
    if not npi:
        match = re.fullmatch(
            r"\s*NPI\s*:?\s*(\d{10})\s*", profile.work_authorization or "",
            re.IGNORECASE,
        )
        npi = match.group(1) if match else ""
    return {
        "first_name": (profile.first_name or "").strip(),
        "last_name": (profile.last_name or "").strip(),
        "email": (profile.email or "").strip(),
        "phone": (profile.phone or "").strip(),
        "npi": npi,
        "city": (profile.city or "").strip(),
        "state": (profile.state_code or "").strip(),
        "zip": (profile.zip_code or "").strip(),
        "profession": (profile.profession_type or profile.provider_category or "").strip(),
        "specialty": (profile.specialty or profile.job_category or "").strip(),
        "resume_url": (profile.resume_url or "").strip(),
    }


def enqueue_many(db, user: User, profiles) -> int:
    """Queue a batch with constant-query organization and duplicate lookups."""
    if not getattr(user, "medhunt_nexus_enabled", False):
        return 0
    candidates = [profile for profile in profiles if profile.email or profile.phone]
    if not candidates:
        return 0
    try:
        target = ats_connections.for_user(db, user.user_id, "nexus")
    except Exception:  # an ambiguous/misconfigured connection must not break reveal
        logger.warning("Nexus delivery was not queued: user connection is ambiguous", exc_info=True)
        return 0
    if not target:
        return 0
    employer, integration = target
    profile_ids = [profile.profile_id for profile in candidates]
    existing = set(db.scalars(select(NexusContactDelivery.profile_id).where(
        NexusContactDelivery.integration_id == integration.integration_id,
        NexusContactDelivery.profile_id.in_(profile_ids),
    )).all())
    queued = [profile for profile in candidates if profile.profile_id not in existing]
    if not queued:
        return 0
    try:
        with db.begin_nested():
            db.add_all([
                NexusContactDelivery(
                    integration_id=integration.integration_id,
                    employer_id=employer.employer_id,
                    profile_id=profile.profile_id,
                    actor_user_id=user.user_id,
                    payload=_snapshot(profile),
                    next_attempt_at=utcnow(),
                )
                for profile in queued
            ])
            db.flush()
        return len(queued)
    except IntegrityError:
        # Another request won the uniqueness race. Its delivery is sufficient.
        return 0


def enqueue(db, user: User, profile: Profile) -> bool:
    """Queue one contact without delaying or breaking its reveal."""
    return bool(enqueue_many(db, user, [profile]))


class NexusClient:
    def __init__(self, values: Mapping[str, Any]):
        self.values = values
        self.http = httpx.Client(timeout=httpx.Timeout(45, connect=10))
        self.token = ""

    def close(self):
        self.http.close()

    def _auth(self) -> str:
        method = self.values.get("auth_method", "password")
        if method == "static":
            return str(self.values.get("static_token") or "")
        headers: dict[str, str] = {}
        auth = None
        if self.values.get("client_id") and self.values.get("client_secret"):
            auth = (self.values["client_id"], self.values["client_secret"])
        elif self.values.get("token_basic"):
            basic = str(self.values["token_basic"])
            headers["Authorization"] = basic if basic.lower().startswith("basic ") else f"Basic {basic}"
        payload = {"grant_type": method}
        if method == "password":
            payload.update(username=self.values.get("username"), password=self.values.get("password"))
        if self.values.get("org_code"):
            payload["organizationCode"] = self.values["org_code"]
            headers["organizationCode"] = self.values["org_code"]
        kwargs = {"json": payload} if self.values.get("token_payload_style") == "json" else {"data": payload}
        response = self.http.post(self.values["token_url"], auth=auth, headers=headers, **kwargs)
        response.raise_for_status()
        body = response.json()
        sources = [body, body.get("data") if isinstance(body, dict) else None]
        for source in sources:
            if isinstance(source, dict):
                for key in ("access_token", "acess_token", "accessToken", "token", "jwt"):
                    if source.get(key):
                        return str(source[key])
        raise RuntimeError("Nexus authentication returned no access token")

    def request(self, method: str, path: str, **kwargs):
        if not self.token:
            self.token = self._auth()
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self.token}"
        response = self.http.request(
            method, str(self.values["base_url"]).rstrip("/") + path,
            headers=headers, **kwargs,
        )
        if response.status_code in (401, 403) and self.values.get("auth_method") != "static":
            self.token = self._auth()
            headers["Authorization"] = f"Bearer {self.token}"
            response = self.http.request(
                method, str(self.values["base_url"]).rstrip("/") + path,
                headers=headers, **kwargs,
            )
        response.raise_for_status()
        return response

    @staticmethod
    def _rows(body: Any) -> list[dict]:
        if isinstance(body, list):
            return [row for row in body if isinstance(row, dict)]
        if not isinstance(body, dict):
            raise RuntimeError("Nexus returned an unreadable response")
        for key in ("records", "data", "results", "content", "items"):
            value = body.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
            if isinstance(value, dict):
                nested = NexusClient._rows(value)
                if nested:
                    return nested
        return []

    def search(self, field: str, value: str) -> list[dict]:
        response = self.request(
            "POST", "/api/api-integration/v1/candidates/search",
            json={"pagingSortingDetails": {"start": 0, "maxRowsToFetch": 20}, field: value},
        )
        return self._rows(response.json())

    def master(self, name: str) -> list[dict]:
        response = self.request("GET", f"/api/api-integration/v1/master/{name}")
        return self._rows(response.json())


def _label(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _master_id(client: NexusClient, name: str, candidates: list[str]) -> int | None:
    wanted = {_label(value) for value in candidates if value}
    for row in client.master(name):
        labels = {_label(row.get(key)) for key in ("name", "label", "description", "code", "value")}
        if wanted & labels:
            for key in ("id", f"{name[:-1]}Id", "value"):
                try:
                    return int(row.get(key))
                except (TypeError, ValueError):
                    pass
    return None


def _profile_payload(client: NexusClient, identity: Mapping[str, Any], defaults: Mapping[str, Any]) -> dict:
    profile = dict(defaults)
    state_map = profile.pop("stateIds", {}) or {}
    referral_name = str(profile.pop("referralSourceName", "") or "")
    status_name = str(profile.pop("statusName", "") or "")
    country_name = str(profile.pop("countryName", "") or "")
    profession_name = str(profile.pop("professionName", "") or "")
    specialty_name = str(profile.pop("specialtyName", "") or "")
    profile.update(firstName=identity["first_name"], lastName=identity["last_name"],
                   sendMassEmails=False, sendMassSms=False)
    if identity.get("email"):
        profile.update(email=identity["email"], primaryEmail=identity["email"])
    if identity.get("phone"):
        profile.update(phone=identity["phone"], cellPhone=identity["phone"])
    if identity.get("city"):
        profile["city"] = identity["city"]
    if identity.get("zip"):
        profile["zipCode"] = identity["zip"]
    if str(identity.get("npi") or "").isdigit():
        profile["npi"] = int(identity["npi"])
    state = str(identity.get("state") or "")
    if not profile.get("stateId") and state:
        mapped = next((value for key, value in state_map.items() if _label(key) == _label(state)), None) if isinstance(state_map, dict) else None
        profile["stateId"] = int(mapped) if mapped else _master_id(client, "states", [state])
    if not profile.get("countryId"):
        profile["countryId"] = _master_id(client, "countries", [country_name or "United States", "USA", "US"])
    if not profile.get("statusId"):
        profile["statusId"] = _master_id(client, "candidatestatuses", [status_name or "PROSPECT"])
    if not profile.get("referralSourceId") and referral_name:
        profile["referralSourceId"] = _master_id(client, "referralsources", [referral_name])
    if not profile.get("professionId"):
        pid = _master_id(client, "professions", [profession_name, identity.get("profession", "")])
        if pid:
            profile.update(professionId=pid, professionIds=[pid])
    if not profile.get("specialtyId"):
        sid = _master_id(client, "specialties", [specialty_name, identity.get("specialty", "")])
        if sid:
            profile.update(specialtyId=sid, primarySpecialtyId=sid, specialtyIds=[sid])
    return {key: value for key, value in profile.items() if value is not None and value != ""}


def _pdf(identity: Mapping[str, Any]) -> bytes:
    lines = [
        f"{identity.get('first_name', '')} {identity.get('last_name', '')}".strip(),
        f"NPI: {identity.get('npi') or 'Not provided'}",
        f"Email: {identity.get('email') or 'Not provided'}",
        f"Phone: {identity.get('phone') or 'Not provided'}",
        f"Location: {', '.join(x for x in (identity.get('city'), identity.get('state'), identity.get('zip')) if x)}",
        f"Profession: {identity.get('profession') or 'Healthcare provider'}",
        f"Specialty: {identity.get('specialty') or 'Not provided'}",
    ]
    escaped = [str(line).encode("ascii", "replace").decode().replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in lines]
    stream = "BT /F1 12 Tf 50 750 Td 16 TL " + " ".join(f"({line}) Tj T*" for line in escaped) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(stream.encode())} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data)); data.extend(f"{number} 0 obj\n{obj}\nendobj\n".encode())
    xref = len(data); data.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]: data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return bytes(data)


def _resume_attachment(identity: Mapping[str, Any]) -> tuple[str, bytes]:
    """Use the original PDF when available; otherwise create a safe summary."""
    resume_url = str(identity.get("resume_url") or "").strip()
    if resume_url:
        try:
            key, is_local = storage.key_from_url(resume_url)
            content = storage.download_bytes(key, prefer_local=is_local)
            if content.startswith(b"%PDF-") and len(content) <= 10 * 1024 * 1024:
                return "MedHunt_Provider_Resume.pdf", content
        except Exception:  # missing/non-PDF files use the generated summary
            logger.info("Original resume unavailable for Nexus; using provider summary")
    return "MedHunt_Provider_Profile.pdf", _pdf(identity)


def _candidate_id(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    for source in (body, body.get("data") if isinstance(body, dict) else None):
        if isinstance(source, dict):
            for key in ("candidateId", "candidate_id", "id"):
                if source.get(key) is not None:
                    return str(source[key])
    return None


def _deliver(delivery: NexusContactDelivery, integration: AtsOrganizationIntegration) -> tuple[str, str | None]:
    values = ats_connections.settings(integration)
    identity = delivery.payload or {}
    client = NexusClient(values)
    try:
        # Both checks finish before the first write. Any match stops all writes,
        # including resume uploads and updates to the existing Nexus candidate.
        email_matches = client.search("email", identity["email"]) if identity.get("email") else []
        phone_matches = client.search("phone", identity["phone"]) if identity.get("phone") else []
        if email_matches or phone_matches:
            return "already_present", None
        profile = _profile_payload(client, identity, values.get("default_profile") or {})
        filename, content = _resume_attachment(identity)
        try:
            response = client.request(
                "POST", "/api/api-integration/v1/candidate/webhook/create",
                data={"profileData": json.dumps(profile, separators=(",", ":"))},
                files=[("profile", (filename, content, "application/pdf"))],
            )
        except httpx.HTTPError as exc:
            raise NexusWriteUncertain("Nexus candidate creation needs manual review") from exc
        return "delivered", _candidate_id(response)
    finally:
        client.close()


def process_once() -> bool:
    db = SessionLocal()
    delivery = None
    advisory_key = None
    advisory_lock = False
    try:
        now = utcnow()
        query = (select(NexusContactDelivery)
                 .where(or_(
                     and_(
                         NexusContactDelivery.status.in_(("pending", "retry")),
                         or_(NexusContactDelivery.next_attempt_at.is_(None),
                             NexusContactDelivery.next_attempt_at <= now),
                     ),
                     and_(
                         NexusContactDelivery.status == "processing",
                         NexusContactDelivery.updated_at <= now - timedelta(minutes=15),
                     ),
                 ))
                 .order_by(NexusContactDelivery.created_at).limit(1))
        if db.bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        delivery = db.scalar(query)
        if not delivery:
            return False
        delivery.status = "processing"
        delivery.attempts += 1
        db.commit()
        integration = db.get(AtsOrganizationIntegration, delivery.integration_id)
        if not integration or integration.status != "connected":
            raise RuntimeError("The organization's Nexus connection is unavailable")
        if db.bind.dialect.name == "postgresql":
            # Serialize deliveries per Nexus tenant. Otherwise two web workers
            # could both search before either creates, producing a duplicate.
            advisory_key = f"medhunt-nexus:{delivery.integration_id}"
            advisory_lock = bool(db.scalar(text(
                "SELECT pg_try_advisory_lock(hashtext(:key))"
            ), {"key": advisory_key}))
            if not advisory_lock:
                delivery.status = "retry"
                delivery.next_attempt_at = utcnow() + timedelta(seconds=5)
                return True
        status, candidate_id = _deliver(delivery, integration)
        delivery.status = status
        delivery.nexus_candidate_id = candidate_id
        delivery.completed_at = utcnow()
        delivery.last_error = None
    except NexusWriteUncertain as exc:
        delivery.status = "review"
        delivery.last_error = str(exc)
    except httpx.HTTPStatusError as exc:
        delivery.status = "retry" if delivery.attempts < 5 else "failed"
        delivery.next_attempt_at = utcnow() + timedelta(minutes=min(30, 2 ** delivery.attempts))
        delivery.last_error = f"Nexus returned HTTP {exc.response.status_code}"
    except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
        if delivery is not None:
            delivery.status = "retry" if delivery.attempts < 5 else "failed"
            delivery.next_attempt_at = utcnow() + timedelta(minutes=min(30, 2 ** delivery.attempts))
            delivery.last_error = str(exc)[:500]
    finally:
        if delivery is not None:
            if advisory_lock and advisory_key:
                db.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"),
                           {"key": advisory_key})
            db.commit()
        db.close()
    return True


async def scheduler() -> None:
    while True:
        worked = await asyncio.to_thread(process_once)
        await asyncio.sleep(0.25 if worked else 5.0)
