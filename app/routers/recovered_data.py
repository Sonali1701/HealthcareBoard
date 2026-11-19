"""Recruiter directory for the recovered, merged source records in neondb."""
from __future__ import annotations

import re
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, select, text
from sqlalchemy.exc import SQLAlchemyError

from ..config import settings
from ..database import engine
from ..deps import CurrentUser, DbSession
from ..models import AuditLog, COST_REVEAL_CONTACT, RecoveredDataContactLookupJob
from ..services import credits, quick_sourcer, storage

router = APIRouter(prefix="/api/data-directory", tags=["data-directory"])
_STATE = re.compile(r"^[A-Z]{2}$")
_ZIP = re.compile(r"^\d{5}$")
_CREDENTIAL = re.compile(r"^[A-Z][A-Z0-9+./-]{1,12}$")
_PROFILE_SEARCH = "(coalesce(first_name,'') || ' ' || coalesce(last_name,'') || ' ' || coalesce(specialty,'') || ' ' || coalesce(job_category,'') || ' ' || coalesce(credential,'') || ' ' || coalesce(city,'') || ' ' || coalesce(npi_number,''))"
_LICENSE_SEARCH = "(coalesce(full_name,'') || ' ' || coalesce(first_name,'') || ' ' || coalesce(last_name,'') || ' ' || coalesce(specialty,'') || ' ' || coalesce(job_category,'') || ' ' || coalesce(profession,'') || ' ' || coalesce(city,''))"
_FIELDS = """record_type, source_record_key, provider_id, license_record_id,
    display_name, first_name, last_name, credential, profession, specialty,
    job_category, years_experience, city, state_code, zip_code, npi_number,
    resume_url IS NOT NULL AS has_resume, email IS NOT NULL AS has_email,
    phone IS NOT NULL AS has_phone, license_type, license_status, review_status"""


_CATEGORIES = (
    "APP", "Allied", "Allied Health & Technicians", "Chiropractic", "Dental",
    "Mental & Behavioral Health", "Mental & Behavioural Health", "Nursing",
    "Nutrition", "Optometry", "Pharmacy", "Physician", "Podiatry",
    "Social Work & Community Services", "Therapy",
)
_STATES = tuple("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI "
                "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT "
                "VA WA WV WI WY".split())


@router.get("/facets")
def data_facets(user: CurrentUser):
    _authorize(user)
    return {"categories": _CATEGORIES, "states": _STATES}


class LookupRequest(BaseModel):
    record_keys: list[str] = Field(min_length=1, max_length=50)


def _lookup_payload(job: RecoveredDataContactLookupJob) -> dict:
    result = {"job_id": job.job_id, "record_key": job.source_record_key,
              "status": job.status, "detail": job.detail, "source": job.source,
              "credits_charged": job.credits_charged,
              "credits_remaining": job.credits_remaining}
    if job.status == "completed":
        result.update(email=job.email, phone=job.phone)
    return result


@router.post("/contacts/lookup")
def enqueue_data_lookups(body: LookupRequest, user: CurrentUser, db: DbSession):
    _authorize(user)
    if not quick_sourcer.available():
        raise HTTPException(503, "Contact lookup is unavailable")
    keys = list(dict.fromkeys(body.record_keys))
    if any(not re.fullmatch(r"(?:profile:[0-9a-f-]{36}|license:[0-9]+)", k) for k in keys):
        raise HTTPException(422, "Invalid record key")
    profile_ids = [UUID(k[8:]) for k in keys if k.startswith("profile:")]
    license_ids = [int(k[8:]) for k in keys if k.startswith("license:")]
    records = {}
    if profile_ids:
        query = text('''SELECT provider_id::text AS id, first_name, last_name,
                               NULL::text AS full_name, city, state_code
                        FROM public."New_provider" WHERE provider_id IN :ids''').bindparams(
            bindparam("ids", expanding=True))
        for r in db.execute(query, {"ids": profile_ids}).mappings():
            records["profile:" + r["id"]] = r
    if license_ids:
        query = text('''SELECT license_record_id::text AS id, first_name, last_name,
                               full_name, city, state_code
                        FROM public."New_provider_license" WHERE license_record_id IN :ids''').bindparams(
            bindparam("ids", expanding=True))
        for r in db.execute(query, {"ids": license_ids}).mappings():
            records["license:" + r["id"]] = r
    if len(records) != len(keys):
        raise HTTPException(404, "One or more records are unavailable")
    prior = db.scalars(select(RecoveredDataContactLookupJob).where(
        RecoveredDataContactLookupJob.requester_user_id == user.user_id,
        RecoveredDataContactLookupJob.source_record_key.in_(keys),
        RecoveredDataContactLookupJob.status.in_(("queued", "processing", "completed")),
    ).order_by(RecoveredDataContactLookupJob.created_at.desc())).all()
    existing = {}
    for job in prior:
        existing.setdefault(job.source_record_key, job)
    jobs = []
    for key in keys:
        if key in existing:
            jobs.append(existing[key]); continue
        r = records[key]
        name = " ".join(str(r[v]).strip() for v in ("first_name", "last_name") if r[v] and str(r[v]).strip())
        if len(name.split()) < 2 and r["full_name"]:
            name = str(r["full_name"]).strip()
        if len(name.split()) < 2:
            raise HTTPException(400, f"Record {key} needs a first and last name for contact lookup")
        location = ", ".join(str(r[v]).strip() for v in ("city", "state_code") if r[v] and str(r[v]).strip())
        job = RecoveredDataContactLookupJob(
            requester_user_id=user.user_id, source_record_key=key,
            lookup_name=name, lookup_location=location or None, status="queued")
        db.add(job); jobs.append(job)
    db.commit()
    return {"jobs": [_lookup_payload(job) for job in jobs]}


@router.get("/contacts/lookup/status")
def data_lookup_status(job_ids: str, user: CurrentUser, db: DbSession):
    _authorize(user)
    ids = list(dict.fromkeys(part for part in job_ids.split(",") if part))
    if not ids or len(ids) > 50:
        raise HTTPException(422, "Request between 1 and 50 lookup jobs")
    jobs = db.scalars(select(RecoveredDataContactLookupJob).where(
        RecoveredDataContactLookupJob.job_id.in_(ids),
        RecoveredDataContactLookupJob.requester_user_id == user.user_id,
    )).all()
    return {"jobs": [_lookup_payload(job) for job in jobs]}


@router.get("/contacts/lookup/records")
def data_lookup_records(record_keys: str, user: CurrentUser, db: DbSession):
    _authorize(user)
    keys = list(dict.fromkeys(k for k in record_keys.split(",") if k))
    if not keys or len(keys) > 60 or any(
            not re.fullmatch(r"(?:profile:[0-9a-f-]{36}|license:[0-9]+)", k) for k in keys):
        raise HTTPException(422, "Request between 1 and 60 valid record keys")
    jobs = db.scalars(select(RecoveredDataContactLookupJob).where(
        RecoveredDataContactLookupJob.requester_user_id == user.user_id,
        RecoveredDataContactLookupJob.source_record_key.in_(keys),
    ).order_by(RecoveredDataContactLookupJob.created_at.desc())).all()
    latest = {}
    for job in jobs:
        latest.setdefault(job.source_record_key, job)
    return {"jobs": [_lookup_payload(job) for job in latest.values()]}


def _authorize(user: CurrentUser) -> None:
    if user.role.value not in {"recruiter", "admin"}:
        raise HTTPException(403, "Data is available to recruiters only")


def _conditions(q, category, specialty, state_code, zip_code, radius_mi, resume, contact, source):
    # Rejected records have source evidence that makes them unsuitable for users.
    clauses = ["review_status <> 'rejected'"]
    params = {}
    if category:
        clauses.append("job_category = :category")
        params["category"] = category
    if specialty:
        clauses.append("specialty ILIKE :specialty")
        params["specialty"] = f"%{specialty}%"
    if state_code:
        clauses.append("state_code = :state_code")
        params["state_code"] = state_code
    if zip_code and radius_mi is None:
        clauses.append("zip_code = :zip_code")
        params["zip_code"] = zip_code
    elif zip_code:
        clauses.append("zip_code = ANY(:nearby_zips)")
    if resume == "with":
        clauses.append("resume_url IS NOT NULL")
    elif resume == "without":
        clauses.append("resume_url IS NULL")
    if contact == "with":
        clauses.append("(email IS NOT NULL OR phone IS NOT NULL)")
    elif contact == "without":
        clauses.append("email IS NULL AND phone IS NULL")
    if source:
        clauses.append("record_type = :source")
        params["source"] = source
    return clauses, params


@router.get("")
def search_data(
    user: CurrentUser,
    q: str = Query("", max_length=120),
    category: str = Query("", max_length=120),
    specialty: str = Query("", max_length=120),
    state_code: str = Query("", max_length=2),
    zip_code: str = Query("", max_length=5),
    radius_mi: float | None = Query(None, ge=5, le=250),
    resume: str = Query("", pattern="^(|with|without)$"),
    contact: str = Query("", pattern="^(|with|without)$"),
    source: str = Query("", pattern="^(|profile|license)$"),
    cursor: str = Query("", max_length=50),
    limit: int = Query(40, ge=1, le=60),
):
    _authorize(user)
    state_code = state_code.upper()
    if state_code and (not _STATE.fullmatch(state_code) or state_code not in _STATES):
        raise HTTPException(422, "Use a two-letter state code")
    if q and sum(ch.isalnum() for ch in q) < 3 and not (_CREDENTIAL.fullmatch(q) and q == q.upper()):
        raise HTTPException(422, "Enter at least three letters or a credential")
    if zip_code and not _ZIP.fullmatch(zip_code):
        raise HTTPException(422, "Use a five-digit ZIP code")
    if radius_mi is not None and not zip_code:
        raise HTTPException(422, "Enter a ZIP code to use distance")
    if cursor and not re.fullmatch(r"(?:p:[0-9a-f-]{36}|l:[0-9]+)", cursor):
        raise HTTPException(422, "Invalid cursor")
    clauses, params = _conditions(q.strip(), category.strip(), specialty.strip(),
                                  state_code, zip_code, radius_mi, resume, contact, source)
    # Scan one indexed primary-key range at a time. No full-table COUNT or
    # OFFSET sort is required for the initial page or subsequent pages.
    rows = []
    try:
        with engine.connect() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '30000ms'"))
            if radius_mi is not None:
                center = conn.execute(text("SELECT lat, lng FROM public.data_zip_centroids WHERE zip = :zip"),
                                      {"zip": zip_code}).first()
                if not center:
                    raise HTTPException(422, "This ZIP code has no location coordinates")
                nearby = conn.execute(text('''SELECT zip::text FROM public.data_zip_centroids
                    WHERE 3958.7613 * acos(least(1.0, greatest(-1.0,
                      sin(radians(:lat)) * sin(radians(lat)) +
                      cos(radians(:lat)) * cos(radians(lat)) *
                      cos(radians(lng) - radians(:lng))))) <= :radius'''),
                    {"lat": center.lat, "lng": center.lng, "radius": radius_mi}).scalars().all()
                params["nearby_zips"] = nearby
            for kind, code, pk in (("profile", "p", "provider_id"),
                                   ("license", "l", "license_record_id")):
                if source and source != kind:
                    continue
                if cursor and ((cursor[0] == "l" and code == "p") or
                               (cursor[0] == "p" and code == "l" and len(rows) >= limit + 1)):
                    continue
                filters = clauses + ["record_type = :kind"]
                local = dict(params, kind=kind, take=limit + 1 - len(rows))
                if q.strip():
                    search = q.strip()
                    if _CREDENTIAL.fullmatch(search) and search == search.upper():
                        filters.append("upper(credential) = :code" if kind == "profile"
                                       else "upper(profession) = :code")
                        local["code"] = search
                    else:
                        table = '"New_provider"' if kind == "profile" else '"New_provider_license"'
                        expression = _PROFILE_SEARCH if kind == "profile" else _LICENSE_SEARCH
                        filters.append(f'''{pk} IN (SELECT {pk} FROM public.{table}
                                            WHERE {expression} ILIKE :term ESCAPE E'\\\\')''')
                        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                        local["term"] = f"%{escaped}%"
                if cursor and cursor[0] == code:
                    filters.append(f"{pk} > CAST(:after AS {'uuid' if code == 'p' else 'bigint'})")
                    local["after"] = cursor[2:]
                sql = f'''SELECT {_FIELDS} FROM public."New_provider_unified"
                    WHERE {" AND ".join(filters)} ORDER BY {pk} LIMIT :take'''
                rows.extend(dict(r) for r in conn.execute(text(sql), local).mappings())
                if len(rows) >= limit + 1:
                    break
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Data directory is temporarily unavailable") from exc
    has_next = len(rows) > limit
    page = rows[:limit]
    for row in page:
        row["provider_id"] = str(row["provider_id"]) if row["provider_id"] else None
        # Never return contact values or raw storage locations in a listing.
        row.pop("first_name", None)
        row.pop("last_name", None)
    last = page[-1] if page else None
    next_cursor = (("p:" + last["provider_id"]) if last and last["record_type"] == "profile"
                   else ("l:" + str(last["license_record_id"])) if last else None)
    return {"items": page, "has_next": has_next, "next_cursor": next_cursor}


@router.post("/contact/{provider_id}")
def data_contact(provider_id: str, request: Request, user: CurrentUser, db: DbSession):
    _authorize(user)
    if not re.fullmatch(r"[0-9a-f-]{36}", provider_id):
        raise HTTPException(404, "Record not found")
    row = db.execute(text('''SELECT email, phone, resume_url FROM public."New_provider"
                             WHERE provider_id = :id'''), {"id": provider_id}).mappings().first()
    if not row:
        raise HTTPException(404, "Record not found")
    if not (row["email"] or row["phone"] or row["resume_url"]):
        raise HTTPException(404, "No contact or resume on file")
    charge = {"cost": 0, "balance": None}
    if settings.credits_enabled:
        try:
            charge = credits.charge(db, user.user_id, COST_REVEAL_CONTACT,
                                    entity_type="recovered_profile", entity_id=provider_id,
                                    idempotency_key=f"data-contact:{user.user_id}:profile:{provider_id}",
                                    note="Recovered data contact reveal")
        except credits.InsufficientCredits as exc:
            raise HTTPException(402, f"You need {exc.needed} credit(s) and have {exc.balance}.") from exc
    db.add(AuditLog(actor_user_id=user.user_id, action="recovered_data_contact_released",
                    entity_type="recovered_profile", entity_id=provider_id,
                    ip_address=request.client.host if request.client else None))
    db.commit()
    return {"email": row["email"], "phone": row["phone"],
            "has_resume": bool(row["resume_url"]),
            "credits_charged": charge.get("cost", 0), "credits_remaining": charge.get("balance")}


@router.get("/resume/{provider_id}")
def data_resume(provider_id: str, user: CurrentUser, db: DbSession):
    _authorize(user)
    if not re.fullmatch(r"[0-9a-f-]{36}", provider_id):
        raise HTTPException(404, "Record not found")
    row = db.execute(text('''SELECT resume_url FROM public."New_provider"
                             WHERE provider_id = :id'''), {"id": provider_id}).mappings().first()
    if not row or not row["resume_url"]:
        raise HTTPException(404, "No resume on file")
    # The same paid entitlement covers the résumé and contact. An admin may
    # inspect a recovered file for review without charging a personal account.
    if settings.credits_enabled and user.role.value != "admin" and not credits.already_charged(
            db, f"data-contact:{user.user_id}:profile:{provider_id}"):
        raise HTTPException(403, "Reveal this record's contact first to view its resume")
    key, is_local = storage.key_from_url(row["resume_url"])
    if is_local:
        raise HTTPException(404, "Local resume is unavailable")
    try:
        url = storage.presigned_url(key, expires=120)
    except Exception as exc:
        raise HTTPException(503, "Resume is temporarily unavailable") from exc
    db.add(AuditLog(actor_user_id=user.user_id, action="recovered_data_resume_opened",
                    entity_type="recovered_profile", entity_id=provider_id))
    db.commit()
    return {"url": url}
