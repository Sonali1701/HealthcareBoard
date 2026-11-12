"""Read-only recruiter API for the separate NPI-backed provider directory."""
from __future__ import annotations

import json
import math
import re

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from ..config import settings
from ..deps import CurrentUser

router = APIRouter(prefix="/api/provider-directory", tags=["provider-directory"])
_ZIP = re.compile(r"^\d{5}$")
_STATE = re.compile(r"^[A-Z]{2}$")


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
    if q:
        for index, token in enumerate(q.strip().split()[:6]):
            key = f"q{index}"
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
            params[f"{key}_text"] = "%" + token.casefold() + "%"
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
