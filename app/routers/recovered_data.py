"""Recruiter directory for the recovered, merged source records in neondb."""
from __future__ import annotations

import re
import time
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

# These screen results have direct evidence that identity or role could be
# misleading. Missing résumé, location, category, or specialty alone does not
# withhold a record; those values remain blank rather than being invented.
_UNSAFE_SCREEN = """NOT EXISTS (
    SELECT 1 FROM public."New_provider_resume_screen" AS screen
    WHERE screen.provider_id = {alias}.provider_id
      AND screen.flags ?| ARRAY['missing_name', 'malformed_name',
          'credential_as_surname', 'nonclinical_technical_header',
          'header_category_conflict']::text[]
)"""


_CATEGORIES = (
    "APP", "Allied", "Allied Health & Technicians", "Chiropractic", "Dental",
    "Mental & Behavioral Health", "Mental & Behavioural Health", "Nursing",
    "Nutrition", "Optometry", "Pharmacy", "Physician", "Podiatry",
    "Social Work & Community Services", "Therapy",
)
_STATES = tuple("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI "
                "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT "
                "VA WA WV WI WY".split())
_SPECIALTY_CACHE: dict[str, tuple[float, list[str]]] = {}


@router.get("/facets")
def data_facets(user: CurrentUser):
    _authorize(user)
    return {"categories": _CATEGORIES, "states": _STATES}


@router.get("/specialties")
def data_specialties(user: CurrentUser, category: str = Query("", max_length=120)):
    _authorize(user)
    if category and category not in _CATEGORIES:
        raise HTTPException(422, "Unknown category")
    cached = _SPECIALTY_CACHE.get(category)
    if cached and cached[0] > time.monotonic():
        return {"specialties": cached[1]}
    try:
        with engine.connect() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '20000ms'"))
            values = conn.execute(text('''SELECT specialty FROM public."New_provider_unified"
                WHERE review_status <> 'rejected' AND specialty IS NOT NULL
                  AND btrim(specialty) <> ''
                  AND (:category = '' OR job_category = :category)
                GROUP BY specialty ORDER BY count(*) DESC, specialty LIMIT 250'''),
                {"category": category}).scalars().all()
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Specialties are temporarily unavailable") from exc
    result = sorted(values, key=str.casefold)
    _SPECIALTY_CACHE[category] = (time.monotonic() + 600, result)
    return {"specialties": result}


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
        query = text(f'''SELECT provider_id::text AS id, first_name, last_name,
                               NULL::text AS full_name, city, state_code
                        FROM public."New_provider" AS p
                        WHERE provider_id IN :ids AND review_status <> 'rejected'
                          AND {_UNSAFE_SCREEN.format(alias='p')}''').bindparams(
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


def _conditions(q, category, specialty, state_code, zip_code, radius_mi, resume, contact, source,
                min_experience=None, max_experience=None):
    # Rejected records have source evidence that makes them unsuitable for users.
    clauses = ["review_status <> 'rejected'",
               f"(record_type <> 'profile' OR {_UNSAFE_SCREEN.format(alias='u')})"]
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
    if min_experience is not None:
        clauses.append("years_experience >= :min_experience")
        params["min_experience"] = min_experience
    if max_experience is not None:
        clauses.append("years_experience <= :max_experience")
        params["max_experience"] = max_experience
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
    min_experience: int | None = Query(None, ge=0, le=80),
    max_experience: int | None = Query(None, ge=0, le=80),
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
                                  state_code, zip_code, radius_mi, resume, contact, source,
                                  min_experience, max_experience)
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
                sql = f'''SELECT {_FIELDS} FROM public."New_provider_unified" AS u
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


class DataCopilotQuery(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    context: dict | None = None


_AI_DATA_ROLES = {
    "rn": "Registered Nurse", "registered nurses": "Registered Nurse",
    "np": "Nurse Practitioner", "nurse practitioners": "Nurse Practitioner",
    "fnp": "Family Nurse Practitioner",
    "pt": "Physical Therapist", "physical therapists": "Physical Therapist",
    "ot": "Occupational Therapist", "occupational therapists": "Occupational Therapist",
    "lpn": "Licensed Practical Nurse", "lvn": "Licensed Vocational Nurse",
    "pa": "Physician Assistant", "crna": "Nurse Anesthetist",
}


def _copilot_data_filters(message: str, context: dict | None = None) -> tuple[dict, list[str]]:
    """Translate a natural-language request into filters supported by Data."""
    from .profiles import _copilot_rule_filters
    from .provider_directory import _copilot_directory_filters

    prior = context if isinstance(context, dict) else {}
    if re.search(r"\b(new search|start over|reset|clear filters)\b", message, re.I):
        prior = {}
    allowed = ("q", "category", "specialty", "state_code", "zip_code", "radius_mi",
               "resume", "contact", "min_experience", "max_experience")
    filters = {key: prior[key] for key in allowed if prior.get(key) is not None and prior.get(key) != ""}
    parsed = _copilot_rule_filters(message)
    common, _ = _copilot_directory_filters(message, {
        "q": filters.get("q"), "state_code": filters.get("state_code"),
        "zip": filters.get("zip_code"), "radius_mi": filters.get("radius_mi"),
        "specialty_text": filters.get("specialty"),
    })
    if common.get("q"):
        query = str(common["q"]).strip()
        filters["q"] = _AI_DATA_ROLES.get(query.casefold(), query)
    if parsed.get("npi_number"):
        filters["q"] = parsed["npi_number"]
    if parsed.get("category") in _CATEGORIES:
        filters["category"] = parsed["category"]
    for source_key, target_key in (("state_code", "state_code"), ("zip", "zip_code"),
                                   ("radius_mi", "radius_mi")):
        if common.get(source_key):
            filters[target_key] = common[source_key]
    if common.get("specialty_text"):
        specialty = common["specialty_text"]
        # Source records commonly use ICU/ER/PACU rather than the NPI wording.
        if re.search(r"\bicu\b", message, re.I):
            specialty = "ICU"
        elif re.search(r"\bpacu\b", message, re.I):
            specialty = "PACU"
        elif re.search(r"\ber\b", message, re.I):
            specialty = "Emergency"
        filters["specialty"] = specialty
    if parsed.get("resume_status"):
        filters["resume"] = "with" if parsed["resume_status"] == "with_resume" else "without"
    if parsed.get("contact_available"):
        filters["contact"] = "with"
    for key in ("min_experience", "max_experience"):
        if parsed.get(key) is not None:
            filters[key] = parsed[key]
    notes = []
    if parsed.get("licensed_state") or parsed.get("compact"):
        notes.append("license eligibility")
    if parsed.get("travel_experience") or parsed.get("worked_at"):
        notes.append("work history")
    if parsed.get("contact_available") in {"both", "email", "phone"}:
        notes.append("specific email or phone availability")
    if parsed.get("city") and not filters.get("zip_code"):
        notes.append("city distance (enter a ZIP for radius search)")
    if not filters.get("zip_code"):
        filters.pop("radius_mi", None)
    return filters, notes


@router.post("/copilot")
def copilot_data(body: DataCopilotQuery, user: CurrentUser):
    _authorize(user)
    filters, unsupported = _copilot_data_filters(body.message, body.context)
    if not filters:
        return {"answer": "Try a profession, specialty, state, or ZIP code.",
                "filters": {}, "items": [], "total": 0, "has_next": False}
    page = search_data(
        user=user, q=str(filters.get("q") or "")[:120],
        category=str(filters.get("category") or ""),
        specialty=str(filters.get("specialty") or ""),
        state_code=str(filters.get("state_code") or ""),
        zip_code=str(filters.get("zip_code") or ""),
        radius_mi=filters.get("radius_mi"),
        resume=str(filters.get("resume") or ""),
        contact=str(filters.get("contact") or ""),
        source="", min_experience=filters.get("min_experience"),
        max_experience=filters.get("max_experience"), cursor="", limit=12,
    )
    items = page["items"]
    note = (" I could not filter by " + ", ".join(unsupported) +
            " because these fields are not verified in Data.") if unsupported else ""
    answer = (f"Showing {len(items)} matching Data records" +
              (" (more available)." if page["has_next"] else ".") + note) if items else (
                  "No Data records matched those filters." + note)
    return {"answer": answer, "filters": filters, "items": items,
            "total": len(items), "has_next": page["has_next"]}


@router.post("/contact/{provider_id}")
def data_contact(provider_id: str, request: Request, user: CurrentUser, db: DbSession):
    _authorize(user)
    if not re.fullmatch(r"[0-9a-f-]{36}", provider_id):
        raise HTTPException(404, "Record not found")
    row = db.execute(text(f'''SELECT email, phone, resume_url FROM public."New_provider" AS p
                             WHERE provider_id = :id AND review_status <> 'rejected'
                               AND {_UNSAFE_SCREEN.format(alias='p')}'''),
                     {"id": provider_id}).mappings().first()
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
    row = db.execute(text(f'''SELECT resume_url, first_name, last_name, credential,
                                  headline, specialty, job_category, years_experience,
                                  city, state_code, zip_code, npi_number, work_authorization,
                                  resume_sections, work_history, education, skills,
                                  certifications, licenses, extracted_text
                           FROM public."New_provider" AS p
                             WHERE provider_id = :id AND review_status <> 'rejected'
                               AND {_UNSAFE_SCREEN.format(alias='p')}'''),
                     {"id": provider_id}).mappings().first()
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
    def resume_line(item):
        if isinstance(item, dict):
            return " · ".join(str(value).strip() for value in item.values()
                              if isinstance(value, (str, int, float)) and str(value).strip())
        return str(item).strip()

    sections = {}
    section_names = {
        "summary": "Professional Summary", "professional_summary": "Professional Summary",
        "experience": "Experience", "work_experience": "Experience",
        "education": "Education & Training", "training": "Education & Training",
        "skills": "Skills", "languages": "Languages",
        "certifications": "Certifications & Licensure", "licenses": "Certifications & Licensure",
    }
    for key, value in (row["resume_sections"] or {}).items():
        label = section_names.get(key.casefold().replace(" ", "_"), key)
        lines = value if isinstance(value, list) else [value]
        sections.setdefault(label, []).extend(line for item in lines
                                               if (line := resume_line(item)))
    if not any(sections.values()) and row["extracted_text"]:
        sections["Professional Summary"] = [line.strip() for line in row["extracted_text"].splitlines()
                                             if line.strip()][:120]
    work = [({"title": item.get("title") or item.get("job_title"),
              "employer": item.get("employer") or item.get("employer_name"),
              "location": item.get("location"), "start": item.get("start") or item.get("start_date"),
              "end": item.get("end") or item.get("end_date")}
             if isinstance(item, dict) else {"title": resume_line(item)})
            for item in (row["work_history"] or [])]
    education = [({"degree": item.get("degree") or item.get("qualification"),
                  "field": item.get("field"),
                  "institution": item.get("institution") or item.get("school"),
                  "year": item.get("year")}
                 if isinstance(item, dict) else {"degree": resume_line(item)})
                 for item in (row["education"] or [])]
    skills = [resume_line(item) for item in (row["skills"] or []) if resume_line(item)]
    certifications = [resume_line(item) for item in (row["certifications"] or [])
                      if resume_line(item)]
    if certifications:
        sections.setdefault("Certifications & Licensure", []).extend(certifications)
    source_licenses = [resume_line(item) for item in (row["licenses"] or [])
                       if resume_line(item)]
    if source_licenses:
        sections.setdefault("Certifications & Licensure", []).extend(source_licenses)
    location = ", ".join(str(row[field]) for field in ("city", "state_code", "zip_code") if row[field])
    return {
        "name": " ".join(part for part in (row["first_name"], row["last_name"]) if part) or "Provider",
        "role": row["headline"] or row["specialty"] or row["job_category"],
        "location": location, "credential": row["credential"],
        "years_experience": row["years_experience"], "npi_number": row["npi_number"],
        "work_authorization": row["work_authorization"],
        "sections": sections, "work_history": work, "education": education,
        "skills": skills, "licenses": [],
        "can_download": False, "has_resume_file": False, "withheld": False,
        "original_url": url,
    }
