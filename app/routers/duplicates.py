"""Duplicate detection and merging for the provider directory.

Matching on a single signal is not safe here. Phone alone groups 23 unrelated
candidates who share one agency switchboard; name+city alone groups everyone
whose name the parser read off a location line ("San TX"). A row is only
treated as the same person when BOTH the contact detail and the name agree.

Merging never deletes: the losing rows are hidden and stamped with
`merged_into`, so their résumé, audit trail and pool memberships survive and
the whole operation can be undone.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, text

from ..database import utcnow
from ..deps import CurrentUser, DbSession
from ..models import Profile, TalentPoolMember
from .profiles import _masked_name, _require_provider_directory_access

router = APIRouter(prefix="/api/duplicates", tags=["duplicates"])

MERGE_REASON = "merged_duplicate"


def _require_admin(user: CurrentUser) -> None:
    """Merging and unmerging rewrite the shared directory irreversibly-ish, so
    they are admin-only — not something every recruiter can do to any profile."""
    if user.role.value != "admin":
        raise HTTPException(status_code=403,
                            detail="Merging profiles is an administrator action")

# Same last-10 phone digits AND the same name. Both, never either.
_GROUPS_SQL = """
SELECT right(regexp_replace(phone, '[^0-9]', '', 'g'), 10) AS phone10,
       lower(btrim(first_name)) AS fn,
       lower(btrim(last_name))  AS ln,
       count(*) AS n
  FROM profiles
 WHERE is_listable IS TRUE
   AND merged_into IS NULL
   AND phone IS NOT NULL
   AND length(regexp_replace(phone, '[^0-9]', '', 'g')) >= 10
   AND first_name IS NOT NULL AND btrim(first_name) <> ''
   AND last_name  IS NOT NULL AND btrim(last_name)  <> ''
 GROUP BY 1, 2, 3
HAVING count(*) > 1
 ORDER BY count(*) DESC, 1
 LIMIT :limit OFFSET :offset
"""


class MergeRequest(BaseModel):
    keep_profile_id: str
    merge_profile_ids: list[str]


def _members(db: DbSession, phone10: str, fn: str, ln: str) -> list[Profile]:
    return db.scalars(
        select(Profile).where(
            Profile.is_listable.is_(True),
            Profile.merged_into.is_(None),
            func.right(func.regexp_replace(Profile.phone, "[^0-9]", "", "g"), 10) == phone10,
            func.lower(func.btrim(Profile.first_name)) == fn,
            func.lower(func.btrim(Profile.last_name)) == ln,
        ).order_by(Profile.completion_score.desc().nullslast(), Profile.created_at)
    ).all()


def _score(p: Profile) -> tuple:
    """Richest row wins: completeness, then contact detail, then recency."""
    return (p.completion_score or 0,
            1 if (p.email or "").strip() else 0,
            1 if p.resume_url else 0,
            p.years_experience or 0,
            p.created_at.timestamp() if p.created_at else 0)


@router.get("")
def list_duplicates(user: CurrentUser, db: DbSession,
                    limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
    """Duplicate groups awaiting review, richest candidate first in each."""
    _require_provider_directory_access(user)
    rows = db.execute(text(_GROUPS_SQL), {"limit": limit, "offset": offset}).all()
    total = db.execute(text(
        "SELECT count(*) FROM (" + _GROUPS_SQL.replace(" LIMIT :limit OFFSET :offset", "") + ") g"
    ), {"limit": limit, "offset": offset}).scalar() or 0

    groups = []
    for phone10, fn, ln, n in rows:
        members = _members(db, phone10, fn, ln)
        if len(members) < 2:
            continue
        suggested = max(members, key=_score)
        groups.append({
            "key": f"{phone10}:{fn}:{ln}",
            "phone_last10": phone10,
            "count": len(members),
            "suggested_keep": suggested.profile_id,
            "members": [{
                "profile_id": p.profile_id,
                "masked_name": _masked_name(p),
                "city": p.city, "state_code": p.state_code,
                "profession_type": p.profession_type, "specialty": p.specialty,
                "years_experience": p.years_experience,
                "has_email": bool((p.email or "").strip()),
                "completion_score": p.completion_score or 0,
                "created_at": p.created_at,
            } for p in members],
        })
    return {"items": groups, "total_groups": total}


@router.post("/merge")
def merge_profiles(body: MergeRequest, user: CurrentUser, db: DbSession):
    """Fold duplicates into one surviving profile."""
    _require_admin(user)
    keep = db.get(Profile, body.keep_profile_id)
    if not keep:
        raise HTTPException(status_code=404, detail="Profile to keep was not found")
    losers = [p for p in (db.get(Profile, i) for i in body.merge_profile_ids)
              if p and p.profile_id != keep.profile_id]
    if not losers:
        raise HTTPException(status_code=400, detail="No other profiles to merge")

    filled = []
    for loser in losers:
        # Backfill anything the survivor is missing — never overwrite.
        for field in ("email", "phone", "city", "state_code", "zip_code", "specialty",
                      "profession_type", "provider_category", "headline", "bio",
                      "resume_url", "years_experience", "lat", "lng"):
            if not getattr(keep, field, None) and getattr(loser, field, None):
                setattr(keep, field, getattr(loser, field))
                filled.append(field)
        # Move pool memberships across, skipping any the survivor already has.
        already = set(db.scalars(
            select(TalentPoolMember.pool_id)
            .where(TalentPoolMember.profile_id == keep.profile_id)).all())
        for m in db.scalars(select(TalentPoolMember)
                            .where(TalentPoolMember.profile_id == loser.profile_id)).all():
            if m.pool_id in already:
                db.delete(m)
            else:
                m.profile_id = keep.profile_id
                already.add(m.pool_id)
        loser.is_listable = False
        loser.merged_into = keep.profile_id
        loser.merged_at = utcnow()
        loser.screen_reason = MERGE_REASON

    db.commit()
    return {"kept": keep.profile_id, "merged": [p.profile_id for p in losers],
            "fields_filled": sorted(set(filled))}


@router.post("/unmerge/{profile_id}")
def unmerge(profile_id: str, user: CurrentUser, db: DbSession):
    """Undo a merge for one profile."""
    _require_admin(user)
    p = db.get(Profile, profile_id)
    if not p or not p.merged_into:
        raise HTTPException(status_code=404, detail="That profile is not merged")
    p.is_listable = True
    p.merged_into = None
    p.merged_at = None
    if p.screen_reason == MERGE_REASON:
        p.screen_reason = None
    db.commit()
    return {"restored": profile_id}
