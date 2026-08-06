"""Saved searches, and the alerts they generate.

Notifications on this platform have never fired because nothing ever created
one. A saved search is the natural first producer: the recruiter states their
standing criteria once, and when the directory grows past the count recorded at
the last check, that delta becomes a notification.

Checks run on demand (when the app loads or the user asks), so no scheduler is
required — the recruiter still gets "12 new ICU RNs in Texas since you last
looked" without any background infrastructure.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..database import utcnow
from ..deps import CurrentUser, DbSession
from ..models import Notification, Profile, SavedSearch
from ..models.enums import NotificationType
from .profiles import (
    _is_recruiter_or_admin,
    _provider_conditions,
    _require_provider_directory_access,
)

router = APIRouter(prefix="/api/saved-searches", tags=["saved-searches"])

# Only directory filters may be persisted — anything else is ignored rather
# than passed through to the query builder.
_ALLOWED = {
    "q", "category", "specialty", "license_title", "profession_type", "state_code",
    "city", "zip", "radius_mi", "min_experience", "max_experience",
    "contact_available", "compact", "licensed_state", "worked_at",
    "travel_experience", "american_board", "open_to_work",
}
_NUMERIC = {"radius_mi", "min_experience", "max_experience"}
_JOB_ALLOWED = {"q", "specialty", "profession_type", "job_type", "state_code",
                "city", "pay_min", "is_urgent", "facility"}
_JOB_NUMERIC = {"pay_min"}
_JOB_BOOLEAN = {"is_urgent"}
_BOOLEAN = {"compact", "travel_experience", "open_to_work"}


class SearchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    params: dict = Field(default_factory=dict)
    notify: bool = True
    # "providers" = a recruiter watching for candidates.
    # "jobs"      = a professional watching for roles.
    kind: str = "providers"


class SearchUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    params: Optional[dict] = None
    notify: Optional[bool] = None


def _clean(params: dict, kind: str = "providers") -> dict:
    """Keep only real filters for this side, coerced to the types the query wants."""
    allowed = _JOB_ALLOWED if kind == "jobs" else _ALLOWED
    booleans = _JOB_BOOLEAN if kind == "jobs" else _BOOLEAN
    numerics = _JOB_NUMERIC if kind == "jobs" else _NUMERIC
    out: dict = {}
    for k, v in (params or {}).items():
        if k not in allowed or v in (None, "", []):
            continue
        if k in booleans:
            out[k] = str(v).lower() in {"1", "true", "yes"}
        elif k in numerics:
            try:
                out[k] = float(v) if k in {"radius_mi", "pay_min"} else int(v)
            except (TypeError, ValueError):
                continue
        else:
            out[k] = str(v)
    return out


def _allowed_kind(user: CurrentUser, kind: str) -> str:
    """Recruiters watch candidates; professionals watch jobs."""
    if kind == "jobs":
        return "jobs"
    _require_provider_directory_access(user)      # candidate searches are gated
    return "providers"


def _own(db: DbSession, search_id: str, user: CurrentUser) -> SavedSearch:
    s = db.get(SavedSearch, search_id)
    if not s:
        raise HTTPException(status_code=404, detail="Saved search not found")
    if s.owner_user_id != user.user_id and user.role.value != "admin":
        raise HTTPException(status_code=403, detail="This search belongs to another recruiter")
    return s


def _count_jobs(db: DbSession, params: dict) -> int:
    """How many live roles match a professional's saved criteria."""
    from ..models import JobPosting
    from ..models.enums import JobStatus

    p = _clean(params, "jobs")
    stmt = select(func.count()).select_from(JobPosting).where(
        JobPosting.status == JobStatus.active)
    if p.get("q"):
        stmt = stmt.where(JobPosting.search_text.like(f"%{str(p['q']).lower()}%"))
    for field in ("specialty", "profession_type", "job_type", "facility"):
        if p.get(field):
            stmt = stmt.where(getattr(JobPosting, field) == p[field])
    if p.get("state_code"):
        stmt = stmt.where(JobPosting.state_code == str(p["state_code"]).upper())
    if p.get("city"):
        stmt = stmt.where(JobPosting.city.ilike(f"%{p['city']}%"))
    if p.get("pay_min") is not None:
        stmt = stmt.where(JobPosting.pay_rate_max >= p["pay_min"])
    if p.get("is_urgent") is not None:
        stmt = stmt.where(JobPosting.is_urgent.is_(bool(p["is_urgent"])))
    return db.scalar(stmt) or 0


def _count_matches(db: DbSession, params: dict, kind: str = "providers") -> int:
    if kind == "jobs":
        return _count_jobs(db, params)
    p = _clean(params)
    category = p.pop("category", None)
    conds = _provider_conditions(db, providers_only=True, **p)
    stmt = select(func.count()).select_from(Profile).where(*conds)
    if category:
        stmt = stmt.where(func.lower(Profile.provider_category) == category.lower())
    return db.scalar(stmt) or 0


def _json(s: SavedSearch, matches: Optional[int] = None) -> dict:
    return {
        "search_id": s.search_id,
        "name": s.name,
        "kind": s.kind,
        "params": s.params or {},
        "notify": s.notify,
        "last_count": s.last_count,
        "last_checked_at": s.last_checked_at,
        "matches": matches,
        "created_at": s.created_at,
    }


@router.get("")
def list_searches(user: CurrentUser, db: DbSession, kind: Optional[str] = None):
    stmt = select(SavedSearch).where(SavedSearch.owner_user_id == user.user_id)
    if kind:
        stmt = stmt.where(SavedSearch.kind == kind)
    elif not _is_recruiter_or_admin(user):
        stmt = stmt.where(SavedSearch.kind == "jobs")
    rows = db.scalars(stmt.order_by(SavedSearch.updated_at.desc())).all()
    return {"items": [_json(s) for s in rows]}


@router.post("", status_code=201)
def create_search(body: SearchCreate, user: CurrentUser, db: DbSession):
    kind = _allowed_kind(user, body.kind)
    name = body.name.strip()
    if db.scalar(select(SavedSearch).where(
            SavedSearch.owner_user_id == user.user_id,
            func.lower(SavedSearch.name) == name.lower())):
        raise HTTPException(status_code=409, detail="You already have a search with that name")
    params = _clean(body.params, kind)
    s = SavedSearch(owner_user_id=user.user_id, name=name, params=params,
                    notify=body.notify, kind=kind)
    # Baseline the count now, so the first check reports genuine growth rather
    # than announcing the entire existing directory as "new".
    s.last_count = _count_matches(db, params, kind)
    s.last_checked_at = utcnow()
    db.add(s)
    db.commit()
    db.refresh(s)
    return _json(s, matches=s.last_count)


@router.patch("/{search_id}")
def update_search(search_id: str, body: SearchUpdate, user: CurrentUser, db: DbSession):
    s = _own(db, search_id, user)
    if body.name is not None:
        s.name = body.name.strip()
    if body.params is not None:
        s.params = _clean(body.params, s.kind)
        s.last_count = _count_matches(db, s.params, s.kind)   # re-baseline
    if body.notify is not None:
        s.notify = body.notify
    s.updated_at = utcnow()
    db.commit()
    db.refresh(s)
    return _json(s)


@router.delete("/{search_id}", status_code=204)
def delete_search(search_id: str, user: CurrentUser, db: DbSession):
    db.delete(_own(db, search_id, user))
    db.commit()


@router.get("/{search_id}/matches")
def search_matches(search_id: str, user: CurrentUser, db: DbSession):
    """Current match count, without disturbing the alert baseline."""
    s = _own(db, search_id, user)
    return {"search_id": s.search_id, "matches": _count_matches(db, s.params or {}, s.kind),
            "last_count": s.last_count}


@router.post("/check")
def check_searches(user: CurrentUser, db: DbSession,
                   notify: bool = Query(True, description="Create notifications for growth")):
    """Re-count every saved search and report what grew.

    This is what actually produces notifications on the platform.
    """
    searches = db.scalars(
        select(SavedSearch).where(SavedSearch.owner_user_id == user.user_id)
    ).all()
    results, created = [], 0
    for s in searches:
        current = _count_matches(db, s.params or {}, s.kind)
        baseline = s.last_count if s.last_count is not None else current
        new = max(0, current - baseline)
        if new and s.notify and notify:
            db.add(Notification(
                user_id=user.user_id,
                type=NotificationType.job_match,
                title=f"{new} new {'role' if s.kind == 'jobs' else 'match'}"
                      f"{'' if new == 1 else 's'}: {s.name}",
                body=f"{current} {'roles' if s.kind == 'jobs' else 'candidates'} "
                     f"now match this saved search.",
                data={"search_id": s.search_id, "new": new, "total": current},
            ))
            created += 1
        s.last_count = current
        s.last_checked_at = utcnow()
        results.append({"search_id": s.search_id, "name": s.name,
                        "kind": s.kind, "matches": current, "new": new})
    db.commit()
    return {"checked": len(searches), "notifications_created": created,
            "results": results}
