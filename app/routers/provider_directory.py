"""Read-only recruiter API for the separate NPI-backed provider directory."""
from __future__ import annotations

import json
import math
import re

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from ..config import settings
from ..deps import CurrentUser, DbSession
from ..models import ProviderDirectoryContactLookupJob
from ..services import quick_sourcer

router = APIRouter(prefix="/api/provider-directory", tags=["provider-directory"])
_ZIP = re.compile(r"^\d{5}$")
_STATE = re.compile(r"^[A-Z]{2}$")
# Short codes must be resolved before broad text matching: RN must not match
# letters inside "Internal Medicine", and MD must not match a person's name.
_CREDENTIAL_SEARCHES = frozenset({
    "MD", "DO", "RN", "NP", "FNP", "LPN", "LVN", "CNA", "PA", "CRNA",
    "CNM", "DNP", "DPT", "PT", "OT", "DDS", "DMD", "DPM", "PHARMD",
    "RPH", "PHD",
})
# These aliases describe professions in the NPI taxonomy. A credential alone
# is not enough to identify a profession: RN appears on some NP and CRNA rows.
# Prefixes include the taxonomy's specialty variants; codes select one role.
_ROLE_SEARCHES = {
    "rn": ("prefix", "163W"), "registered nurse": ("prefix", "163W"),
    "registered nurses": ("prefix", "163W"),
    "np": ("prefix", "363L"), "nurse practitioner": ("prefix", "363L"),
    "nurse practitioners": ("prefix", "363L"),
    "fnp": ("code", "363LF0000X"),
    "family nurse practitioner": ("code", "363LF0000X"),
    "pa": ("prefix", "363A"), "physician assistant": ("prefix", "363A"),
    "crna": ("code", "367500000X"),
    "certified registered nurse anesthetist": ("code", "367500000X"),
    "nurse anesthetist": ("code", "367500000X"),
    "lpn": ("prefix", "164W"), "licensed practical nurse": ("prefix", "164W"),
    "lvn": ("prefix", "164X"), "licensed vocational nurse": ("prefix", "164X"),
    "pt": ("prefix", "2251"), "physical therapist": ("prefix", "2251"),
    "ot": ("prefix", "225X"), "occupational therapist": ("prefix", "225X"),
    "pta": ("prefix", "2252"),
    "physical therapy assistant": ("prefix", "2252"),
    "physical therapist assistant": ("prefix", "2252"),
    "ota": ("prefix", "224Z"),
    "occupational therapy assistant": ("prefix", "224Z"),
    "slp": ("prefix", "235Z"),
    "speech language pathologist": ("prefix", "235Z"),
}
_CREDENTIAL_PHRASES = {
    "certified nursing assistant": "CNA",
    "certified nurse midwife": "CNM",
}


def _search_role(q: str | None) -> tuple[str, str] | None:
    if not q:
        return None
    phrase = re.sub(r"[\s-]+", " ", q.casefold()).strip()
    return _ROLE_SEARCHES.get(phrase) or _ROLE_SEARCHES.get(
        re.sub(r"[^a-z0-9]", "", phrase)
    )


class DirectoryContactLookupRequest(BaseModel):
    npis: list[str] = Field(min_length=1, max_length=50)


def _contact_lookup_payload(job: ProviderDirectoryContactLookupJob) -> dict:
    payload = {
        "job_id": job.job_id, "npi": job.npi, "status": job.status,
        "found": job.found, "detail": job.detail, "source": job.source,
        "credits_charged": job.credits_charged,
        "credits_remaining": job.credits_remaining,
    }
    # Contact values are only returned on a completed job and only to its owner.
    if job.status == "completed":
        payload.update({"email": job.email, "phone": job.phone})
    return payload


def _normalized_database_url(raw: str):
    if raw.startswith("postgres://"):
        raw = "postgresql+psycopg://" + raw[len("postgres://"):]
    elif raw.startswith("postgresql://"):
        raw = "postgresql+psycopg://" + raw[len("postgresql://"):]
    return make_url(raw)


def _directory_engine():
    """Use an explicit directory URL, or the app's Neon role on its new DB."""
    raw = settings.provider_directory_database_url or settings.database_url
    url = _normalized_database_url(raw)
    if url.get_backend_name() != "postgresql":
        return None
    if not settings.provider_directory_database_url:
        url = url.set(database=settings.provider_directory_database_name)
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=180,
        pool_size=5,
        max_overflow=10,
        connect_args={"connect_timeout": 10, "keepalives": 1,
                      "keepalives_idle": 30, "keepalives_interval": 10,
                      "keepalives_count": 5},
    )


_engine = _directory_engine()


def _authorize(user: CurrentUser) -> None:
    if user.role.value not in {"recruiter", "admin"}:
        raise HTTPException(status_code=403, detail="Providers are available to recruiters only")


def _conditions(*, q: str | None, specialty: str | None,
                state_code: str | None, zip_code: str | None,
                radius_mi: float | None, center: tuple[float, float] | None):
    clauses = ["p.status = 'ACTIVE'"]
    params: dict[str, object] = {}
    if specialty:
        clauses.append("p.specialty = :specialty")
        params["specialty"] = specialty
    if state_code:
        clauses.append("p.state_code = :state_code")
        params["state_code"] = state_code
    if zip_code and radius_mi is None:
        clauses.append("p.zip_code = :zip_code")
        params["zip_code"] = zip_code
    role = _search_role(q)
    credential_query = q.strip().upper() if q else ""
    credential_query = _CREDENTIAL_PHRASES.get(q.strip().casefold(), credential_query) if q else ""
    if role:
        kind, value = role
        if kind == "prefix":
            clauses.append("left(p.taxonomy_code, 4) = :role_prefix")
            params["role_prefix"] = value
        else:
            clauses.append("p.taxonomy_code = :role_code")
            params["role_code"] = value
    elif credential_query in _CREDENTIAL_SEARCHES:
        clauses.append("upper(btrim(p.credential)) = :credential")
        params["credential"] = credential_query
    elif q:
        for index, token in enumerate(q.strip().split()[:6]):
            key = f"q{index}"
            # Two-letter fragments are too broad for an infix specialty match.
            # For example, RN occurs inside "Internal" and pulls in physicians.
            specialty_match = (token.casefold() + "%" if len(token) < 3
                               else "%" + token.casefold() + "%")
            clauses.append(
                f"(lower(p.first_name) LIKE :{key}_name "
                f"OR lower(p.middle_name) LIKE :{key}_name "
                f"OR lower(p.last_name) LIKE :{key}_name "
                f"OR lower(p.specialty) LIKE :{key}_text "
                f"OR lower(p.city) LIKE :{key}_name "
                f"OR p.npi LIKE :{key}_npi "
                f"OR p.taxonomy_code LIKE :{key}_npi)"
            )
            params[f"{key}_name"] = token.casefold() + "%"
            params[f"{key}_text"] = specialty_match
            params[f"{key}_npi"] = token + "%"
    if radius_mi is not None and zip_code:
        if center is None:
            clauses.append("FALSE")
        else:
            lat, lng = center
            lat_delta = radius_mi / 69.0
            cos_lat = max(0.01, abs(math.cos(math.radians(lat))))
            lng_delta = min(180.0, radius_mi / (69.0 * cos_lat))
            clauses.extend((
                "p.lat BETWEEN :min_lat AND :max_lat",
                "p.lng BETWEEN :min_lng AND :max_lng",
                "(3958.7613 * acos(least(1.0, greatest(-1.0, "
                "cos(radians(:center_lat)) * cos(radians(p.lat)) * "
                "cos(radians(p.lng - :center_lng)) + "
                "sin(radians(:center_lat)) * sin(radians(p.lat)))))) <= :radius_mi",
            ))
            params.update({
                "min_lat": lat - lat_delta, "max_lat": lat + lat_delta,
                "min_lng": lng - lng_delta, "max_lng": lng + lng_delta,
                "center_lat": lat, "center_lng": lng,
                "radius_mi": radius_mi,
            })
    return " AND ".join(clauses), params


def _engine_or_503():
    if _engine is None:
        raise HTTPException(status_code=503, detail="NPI provider directory is not configured")
    return _engine


def _indexed_expression_estimate(conn, index_name: str, column: str,
                                 value: str) -> int | None:
    """Use a partial index's sampled frequency for a fast role/degree estimate.

    PostgreSQL's EXPLAIN gives the same default row estimate for every value
    of this expression, even after ANALYZE. The index statistics preserve the
    distinct frequencies of common values without counting millions of
    matching rows on every page request.
    """
    return conn.execute(text("""
        SELECT round((s.most_common_freqs[
            array_position(s.most_common_vals::text::text[], :value)
        ] * c.reltuples)::numeric)::bigint
        FROM pg_stats s
        JOIN pg_class c
          ON c.oid = to_regclass(:index_name)
        WHERE s.schemaname = current_schema()
          AND s.tablename = :index_name
          AND s.attname = :column
    """), {"index_name": index_name, "column": column,
           "value": value}).scalar_one_or_none()


@router.get("")
def search_provider_directory(
    user: CurrentUser,
    q: str | None = Query(None, max_length=120),
    specialty: str | None = Query(None, max_length=160),
    state_code: str | None = Query(None, min_length=2, max_length=2),
    zip: str | None = Query(None, min_length=5, max_length=5),
    radius_mi: float | None = Query(None, ge=5, le=250),
    count: bool = False,
    limit: int = Query(60, ge=1, le=100),
    offset: int = Query(0, ge=0, le=1000000),
):
    _authorize(user)
    if state_code and not _STATE.fullmatch(state_code.upper()):
        raise HTTPException(status_code=422, detail="Use a two-letter state code")
    if zip and not _ZIP.fullmatch(zip):
        raise HTTPException(status_code=422, detail="Use a five-digit ZIP code")
    if radius_mi is not None and not zip:
        raise HTTPException(status_code=422, detail="A ZIP code is required for distance search")

    engine = _engine_or_503()
    try:
        with engine.connect() as conn:
            center = None
            if zip and radius_mi is not None:
                point = conn.execute(text(
                    "SELECT lat, lng FROM zip_centroids WHERE zip = :zip"
                ), {"zip": zip}).first()
                center = (float(point.lat), float(point.lng)) if point else None
            where, params = _conditions(
                q=q, specialty=specialty, state_code=state_code.upper() if state_code else None,
                zip_code=zip, radius_mi=radius_mi, center=center,
            )
            rows = conn.execute(text(f"""
                SELECT npi, first_name, middle_name, last_name, credential,
                       status, specialty, taxonomy_code, city, state_code,
                       zip_code
                FROM provider_directory p
                WHERE {where}
                ORDER BY lower(last_name), lower(first_name), npi
                LIMIT :take OFFSET :offset
            """), {**params, "take": limit + 1, "offset": offset}).mappings().all()
            has_next = len(rows) > limit
            items = [dict(row) for row in rows[:limit]]
            minimum_total = offset + len(items) + (1 if has_next else 0)
            estimated_total = None
            estimate_is_lower_bound = False
            total = None
            if count:
                total = conn.execute(text(
                    f"SELECT count(*) FROM provider_directory p WHERE {where}"
                ), params).scalar_one()
            elif not has_next and (offset == 0 or items):
                # The last page already tells us the exact number of matches.
                total = offset + len(items)
                estimated_total = total
            elif "credential" in params or "role_prefix" in params:
                # EXPLAIN estimates every value of these expressions as the
                # same 0.5% of the table. Use sampled frequencies for an
                # unfiltered search; otherwise show a truthful lower bound.
                if not (specialty or state_code or zip):
                    if "credential" in params:
                        estimated_total = _indexed_expression_estimate(
                            conn, "provider_directory_active_credential_name_idx",
                            "upper", params["credential"],
                        )
                    else:
                        estimated_total = _indexed_expression_estimate(
                            conn, "provider_directory_active_role_name_idx",
                            "left", params["role_prefix"],
                        )
                if estimated_total is None:
                    estimated_total = minimum_total
                    estimate_is_lower_bound = has_next
                elif estimated_total < minimum_total:
                    estimated_total = minimum_total
                    estimate_is_lower_bound = has_next
            else:
                plan = conn.execute(text(
                    f"EXPLAIN (FORMAT JSON) SELECT npi FROM provider_directory p WHERE {where}"
                ), params).scalar_one()
                if isinstance(plan, str):
                    plan = json.loads(plan)
                estimated_total = int(plan[0]["Plan"]["Plan Rows"])
                if estimated_total < minimum_total:
                    estimated_total = minimum_total
                    estimate_is_lower_bound = has_next
        return {"items": items, "total": total,
                "estimated_total": estimated_total, "estimate_is_lower_bound": estimate_is_lower_bound,
                "has_next": has_next}
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Provider directory is temporarily unavailable") from exc


@router.get("/facets")
def provider_directory_facets(user: CurrentUser):
    _authorize(user)
    engine = _engine_or_503()
    try:
        with engine.connect() as conn:
            specialties = conn.execute(text(
                "SELECT specialty FROM provider_directory GROUP BY specialty ORDER BY specialty"
            )).scalars().all()
            states = conn.execute(text(
                "SELECT state_code FROM provider_directory GROUP BY state_code ORDER BY state_code"
            )).scalars().all()
            estimate = conn.execute(text("""
                SELECT greatest(0, reltuples)::bigint
                FROM pg_class WHERE oid = 'provider_directory'::regclass
            """)).scalar_one()
        return {"estimated_total": int(estimate), "specialties": specialties,
                "states": states, "source": "NPI Registry"}
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Provider directory is temporarily unavailable") from exc


@router.post("/contacts/lookup")
def enqueue_directory_contact_lookups(
    body: DirectoryContactLookupRequest, user: CurrentUser, db: DbSession,
):
    """Queue up to 50 contact searches using server-verified NPI directory data."""
    _authorize(user)
    if not quick_sourcer.available():
        raise HTTPException(status_code=503, detail="Contact lookup is not switched on for this deployment.")
    npis = list(dict.fromkeys(str(npi).strip() for npi in body.npis))
    if any(not re.fullmatch(r"\d{10}", npi) for npi in npis):
        raise HTTPException(status_code=422, detail="Each NPI must contain 10 digits.")
    engine = _engine_or_503()
    try:
        with engine.connect() as conn:
            query = text("""
                SELECT npi, first_name, middle_name, last_name, city, state_code
                FROM provider_directory
                WHERE npi IN :npis AND status = 'ACTIVE'
            """).bindparams(bindparam("npis", expanding=True))
            rows = conn.execute(query, {"npis": npis}).mappings().all()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Provider directory is temporarily unavailable") from exc
    provider_by_npi = {r["npi"]: r for r in rows}
    if len(provider_by_npi) != len(npis):
        raise HTTPException(status_code=404, detail="One or more selected providers are unavailable.")

    jobs: list[ProviderDirectoryContactLookupJob] = []
    prior_jobs = db.scalars(select(ProviderDirectoryContactLookupJob).where(
        ProviderDirectoryContactLookupJob.requester_user_id == user.user_id,
        ProviderDirectoryContactLookupJob.npi.in_(npis),
        ProviderDirectoryContactLookupJob.status.in_(("queued", "processing", "completed")),
    ).order_by(ProviderDirectoryContactLookupJob.created_at.desc())).all()
    prior_by_npi = {}
    for prior_job in prior_jobs:
        prior_by_npi.setdefault(prior_job.npi, prior_job)
    for npi in npis:
        prior = prior_by_npi.get(npi)
        if prior:
            jobs.append(prior)
            continue
        p = provider_by_npi[npi]
        name = " ".join(str(v).strip() for v in (
            p["first_name"], p["middle_name"], p["last_name"]
        ) if v and str(v).strip())
        if len(name.split()) < 2:
            raise HTTPException(status_code=400, detail=f"Provider {npi} needs a first and last name for lookup.")
        location = ", ".join(str(v).strip() for v in (p["city"], p["state_code"]) if v and str(v).strip())
        job = ProviderDirectoryContactLookupJob(
            requester_user_id=user.user_id, npi=npi, lookup_name=name,
            lookup_location=location or None, status="queued",
        )
        db.add(job)
        jobs.append(job)
    db.commit()
    for job in jobs:
        db.refresh(job)
    return {"jobs": [_contact_lookup_payload(job) for job in jobs]}


@router.get("/contacts/lookup/status")
def directory_contact_lookup_status(job_ids: str, user: CurrentUser, db: DbSession):
    _authorize(user)
    ids = list(dict.fromkeys(part for part in job_ids.split(",") if part))
    if not ids or len(ids) > 50:
        raise HTTPException(status_code=422, detail="Request between 1 and 50 lookup jobs.")
    found = db.scalars(select(ProviderDirectoryContactLookupJob).where(
        ProviderDirectoryContactLookupJob.job_id.in_(ids),
        ProviderDirectoryContactLookupJob.requester_user_id == user.user_id,
    )).all()
    by_id = {job.job_id: job for job in found}
    return {"jobs": [_contact_lookup_payload(by_id[job_id]) for job_id in ids if job_id in by_id]}
