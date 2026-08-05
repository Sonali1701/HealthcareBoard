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
from .profiles import _provider_conditions, _require_provider_directory_access

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
_BOOLEAN = {"compact", "travel_experience", "open_to_work"}


class SearchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    params: dict = Field(default_factory=dict)
    notify: bool = True


class SearchUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    params: Optional[dict] = None
    notify: Optional[bool] = None


def _clean(params: dict) -> dict:
    """Keep only real directory filters, coerced to the types the query wants."""
    out: dict = {}
    for k, v in (params or {}).items():
        if k not in _ALLOWED or v in (None, "", []):
            continue
        if k in _BOOLEAN:
            out[k] = str(v).lower() in {"1", "true", "yes"}
        elif k in _NUMERIC:
            try:
                out[k] = float(v) if k == "radius_mi" else int(v)
            except (TypeError, ValueError):
                continue
        else:
            out[k] = str(v)
    return out


def _own(db: DbSession, search_id: str, user: CurrentUser) -> SavedSearch:
    s = db.get(SavedSearch, search_id)
    if not s:
        raise HTTPException(status_code=404, detail="Saved search not found")
    if s.owner_user_id != user.user_id and user.role.value != "admin":
        raise HTTPException(status_code=403, detail="This search belongs to another recruiter")
    return s


def _count_matches(db: DbSession, params: dict) -> int:
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
        "params": s.params or {},
        "notify": s.notify,
        "last_count": s.last_count,
        "last_checked_at": s.last_checked_at,
        "matches": matches,
        "created_at": s.created_at,
    }


@router.get("")
def list_searches(user: CurrentUser, db: DbSession):
    _require_provider_directory_access(user)
    rows = db.scalars(
        select(SavedSearch).where(SavedSearch.owner_user_id == user.user_id)
        .order_by(SavedSearch.updated_at.desc())
    ).all()
    return {"items": [_json(s) for s in rows]}


@router.post("", status_code=201)
def create_search(body: SearchCreate, user: CurrentUser, db: DbSession):
    _require_provider_directory_access(user)
    name = body.name.strip()
    if db.scalar(select(SavedSearch).where(
            SavedSearch.owner_user_id == user.user_id,
            func.lower(SavedSearch.name) == name.lower())):
        raise HTTPException(status_code=409, detail="You already have a search with that name")
    params = _clean(body.params)
    s = SavedSearch(owner_user_id=user.user_id, name=name, params=params,
                    notify=body.notify)
    # Baseline the count now, so the first check reports genuine growth rather
    # than announcing the entire existing directory as "new".
    s.last_count = _count_matches(db, params)
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
        s.params = _clean(body.params)
        s.last_count = _count_matches(db, s.params)   # re-baseline on new criteria
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
    return {"search_id": s.search_id, "matches": _count_matches(db, s.params or {}),
            "last_count": s.last_count}


@router.post("/check")
def check_searches(user: CurrentUser, db: DbSession,
                   notify: bool = Query(True, description="Create notifications for growth")):
    """Re-count every saved search and report what grew.

    This is what actually produces notifications on the platform.
    """
    _require_provider_directory_access(user)
    searches = db.scalars(
        select(SavedSearch).where(SavedSearch.owner_user_id == user.user_id)
    ).all()
    results, created = [], 0
    for s in searches:
        current = _count_matches(db, s.params or {})
        baseline = s.last_count if s.last_count is not None else current
        new = max(0, current - baseline)
        if new and s.notify and notify:
            db.add(Notification(
                user_id=user.user_id,
                type=NotificationType.job_match,
                title=f"{new} new match{'' if new == 1 else 'es'}: {s.name}",
                body=f"{current} candidates now match this saved search.",
                data={"search_id": s.search_id, "new": new, "total": current},
            ))
            created += 1
        s.last_count = current
        s.last_checked_at = utcnow()
        results.append({"search_id": s.search_id, "name": s.name,
                        "matches": current, "new": new})
    db.commit()
    return {"checked": len(searches), "notifications_created": created,
            "results": results}
