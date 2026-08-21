"""Talent pools — recruiter shortlists built from the provider directory.

Pools are private to their owner. Members are returned through the SAME identity
masking as the directory, so shortlisting a candidate never reveals a name or
contact detail that has not been explicitly released.
"""
from __future__ import annotations

import csv
import io
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import defer

from ..database import utcnow
from ..deps import CurrentUser, DbSession
from ..models import (
    POOL_STAGES,
    Employer,
    EmployerMember,
    Profile,
    TalentPool,
    TalentPoolMember,
    User,
)
from .profiles import (
    _profile_card,
    _released_profile_ids,
    _require_provider_directory_access,
)

router = APIRouter(prefix="/api/pools", tags=["talent-pools"])


# --- Schemas ---------------------------------------------------------------

class PoolCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: Optional[str] = None
    job_id: Optional[str] = None
    color: str = "blue"
    visibility: str = "private"      # "private" | "team"


class PoolUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = None
    job_id: Optional[str] = None
    color: Optional[str] = None
    visibility: Optional[str] = None


class MemberAdd(BaseModel):
    profile_id: Optional[str] = None
    profile_ids: list[str] = Field(default_factory=list)
    note: Optional[str] = None
    stage: str = "sourced"


class MemberUpdate(BaseModel):
    stage: Optional[str] = None
    note: Optional[str] = None


class PoolEmailIn(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    stage: Optional[str] = None      # optionally email just one stage of the pool


def _recruiter_identity(db: DbSession, user: CurrentUser) -> tuple[str, str]:
    """(from_label, reply_to) for a recruiter's outreach email."""
    emp = db.scalar(select(Employer).where(Employer.owner_user_id == user.user_id))
    if not emp:
        member = db.scalar(select(EmployerMember).where(EmployerMember.user_id == user.user_id))
        emp = db.get(Employer, member.employer_id) if member else None
    return (emp.org_name if emp else "A recruiter on HealthBoard"), user.email


# --- Helpers ---------------------------------------------------------------

def _teammates(db: DbSession, user: CurrentUser) -> list[str]:
    """Everyone at the same agency, so a shared pool is genuinely shared."""
    from ..models import Employer, EmployerMember

    employer_ids = list({
        *db.scalars(select(Employer.employer_id)
                    .where(Employer.owner_user_id == user.user_id)).all(),
        *db.scalars(select(EmployerMember.employer_id)
                    .where(EmployerMember.user_id == user.user_id)).all(),
    })
    if not employer_ids:
        return [user.user_id]
    mates = {
        *db.scalars(select(Employer.owner_user_id)
                    .where(Employer.employer_id.in_(employer_ids))).all(),
        *db.scalars(select(EmployerMember.user_id)
                    .where(EmployerMember.employer_id.in_(employer_ids))).all(),
        user.user_id,
    }
    return [m for m in mates if m]


def _pool_or_404(db: DbSession, pool_id: str, user: CurrentUser) -> TalentPool:
    pool = db.get(TalentPool, pool_id)
    if not pool:
        raise HTTPException(status_code=404, detail="Pool not found")
    if pool.owner_user_id == user.user_id or user.role.value == "admin":
        return pool
    # A pool shared with the agency is workable by the whole desk.
    if pool.visibility == "team" and pool.owner_user_id in _teammates(db, user):
        return pool
    raise HTTPException(status_code=403, detail="This pool belongs to another recruiter")


def _check_stage(stage: str) -> str:
    if stage not in POOL_STAGES:
        raise HTTPException(status_code=400,
                            detail=f"stage must be one of {', '.join(POOL_STAGES)}")
    return stage


def _counts(db: DbSession, pool_ids: list[str]) -> dict[str, dict]:
    """Member totals and per-stage breakdown for a batch of pools."""
    if not pool_ids:
        return {}
    out: dict[str, dict] = {p: {"total": 0, "stages": {}} for p in pool_ids}
    for pid, stage, n in db.execute(
        select(TalentPoolMember.pool_id, TalentPoolMember.stage, func.count())
        .where(TalentPoolMember.pool_id.in_(pool_ids))
        .group_by(TalentPoolMember.pool_id, TalentPoolMember.stage)
    ).all():
        out[pid]["total"] += n
        out[pid]["stages"][stage] = n
    return out


def _pool_json(pool: TalentPool, counts: dict, jobs: dict | None = None) -> dict:
    c = counts.get(pool.pool_id) or {"total": 0, "stages": {}}
    return {
        "pool_id": pool.pool_id,
        "name": pool.name,
        "description": pool.description,
        "job_id": pool.job_id,
        "job_title": (jobs or {}).get(pool.job_id) if pool.job_id else None,
        "color": pool.color,
        "visibility": pool.visibility,
        "owner_user_id": pool.owner_user_id,
        "member_count": c["total"],
        "stages": c["stages"],
        "created_at": pool.created_at,
        "updated_at": pool.updated_at,
    }


# --- Pools -----------------------------------------------------------------

@router.get("")
def list_pools(user: CurrentUser, db: DbSession):
    _require_provider_directory_access(user)
    team = _teammates(db, user)
    pools = db.scalars(
        select(TalentPool).where(
            or_(TalentPool.owner_user_id == user.user_id,
                and_(TalentPool.visibility == "team",
                     TalentPool.owner_user_id.in_(team))))
        .order_by(TalentPool.updated_at.desc())
    ).all()
    counts = _counts(db, [p.pool_id for p in pools])
    from ..models import JobPosting
    job_ids = [p.job_id for p in pools if p.job_id]
    jobs = {j.job_id: j.title for j in db.scalars(
        select(JobPosting).where(JobPosting.job_id.in_(job_ids)))} if job_ids else {}
    return {"items": [_pool_json(p, counts, jobs) for p in pools], "stages": list(POOL_STAGES)}


@router.post("", status_code=201)
def create_pool(body: PoolCreate, user: CurrentUser, db: DbSession):
    _require_provider_directory_access(user)
    name = body.name.strip()
    exists = db.scalar(
        select(TalentPool).where(TalentPool.owner_user_id == user.user_id,
                                 func.lower(TalentPool.name) == name.lower())
    )
    if exists:
        raise HTTPException(status_code=409, detail="You already have a pool with that name")
    pool = TalentPool(owner_user_id=user.user_id, name=name,
                      description=body.description, job_id=body.job_id,
                      color=body.color or "blue",
                      visibility="team" if body.visibility == "team" else "private")
    db.add(pool)
    db.commit()
    db.refresh(pool)
    return _pool_json(pool, {})


@router.patch("/{pool_id}")
def update_pool(pool_id: str, body: PoolUpdate, user: CurrentUser, db: DbSession):
    pool = _pool_or_404(db, pool_id, user)
    if body.name is not None:
        pool.name = body.name.strip()
    if body.description is not None:
        pool.description = body.description
    if body.job_id is not None:
        pool.job_id = body.job_id or None
    if body.color is not None:
        pool.color = body.color
    if body.visibility is not None:
        pool.visibility = "team" if body.visibility == "team" else "private"
    pool.updated_at = utcnow()
    db.commit()
    db.refresh(pool)
    return _pool_json(pool, _counts(db, [pool.pool_id]))


@router.delete("/{pool_id}", status_code=204)
def delete_pool(pool_id: str, user: CurrentUser, db: DbSession):
    pool = _pool_or_404(db, pool_id, user)
    db.execute(delete(TalentPoolMember).where(TalentPoolMember.pool_id == pool_id))
    db.delete(pool)
    db.commit()


# --- Membership ------------------------------------------------------------

@router.get("/{pool_id}/members")
def list_members(pool_id: str, user: CurrentUser, db: DbSession,
                 stage: Optional[str] = Query(None)):
    pool = _pool_or_404(db, pool_id, user)
    # The résumé JSON blobs are large and unused by the card — don't ship them.
    stmt = (select(TalentPoolMember, Profile)
            .join(Profile, Profile.profile_id == TalentPoolMember.profile_id)
            .options(defer(Profile.resume_sections), defer(Profile.education))
            .where(TalentPoolMember.pool_id == pool.pool_id))
    if stage:
        stmt = stmt.where(TalentPoolMember.stage == _check_stage(stage))
    rows = db.execute(stmt.order_by(TalentPoolMember.created_at.desc())).all()
    released = _released_profile_ids(db, user, [p.profile_id for _, p in rows])
    items = []
    for member, profile in rows:
        card = _profile_card(
            profile,
            released=(profile.profile_id in released
                      or bool(profile.user_id and profile.user_id == user.user_id)),
        )
        card |= {"member_id": member.member_id, "stage": member.stage,
                 "note": member.note, "added_at": member.created_at}
        items.append(card)
    return {"pool": _pool_json(pool, _counts(db, [pool.pool_id])),
            "items": items, "stages": list(POOL_STAGES)}


@router.post("/{pool_id}/email")
def email_pool(pool_id: str, body: PoolEmailIn, user: CurrentUser, db: DbSession):
    """Send one outreach email to every reachable member of a pool.

    Only members whose contact the recruiter has released and who have an email
    on file are contacted; the rest are counted and skipped. Each candidate's
    reply routes back to the recruiter's own inbox.
    """
    _require_provider_directory_access(user)
    pool = _pool_or_404(db, pool_id, user)
    stmt = (select(Profile).join(TalentPoolMember,
                                 TalentPoolMember.profile_id == Profile.profile_id)
            .where(TalentPoolMember.pool_id == pool.pool_id))
    if body.stage:
        stmt = stmt.where(TalentPoolMember.stage == _check_stage(body.stage))
    profiles = db.scalars(stmt).all()
    released = _released_profile_ids(db, user, [p.profile_id for p in profiles])
    from_label, reply_to = _recruiter_identity(db, user)

    from ..services.email import send_recruiter_message
    sent = skipped_locked = skipped_no_email = 0
    for p in profiles:
        if p.profile_id not in released:
            skipped_locked += 1
            continue
        if not p.email:
            skipped_no_email += 1
            continue
        ok = send_recruiter_message(
            p.email, candidate_name=(p.first_name or "").strip(),
            from_label=from_label, reply_to=reply_to,
            subject=body.subject, message=body.body)
        sent += 1 if ok else 0
    return {"total": len(profiles), "sent": sent,
            "skipped_not_revealed": skipped_locked,
            "skipped_no_email": skipped_no_email}


@router.post("/{pool_id}/members", status_code=201)
def add_members(pool_id: str, body: MemberAdd, user: CurrentUser, db: DbSession):
    """Add one or many profiles. Re-adding an existing member is a no-op, so the
    client can fire this without first checking what is already in the pool."""
    pool = _pool_or_404(db, pool_id, user)
    _check_stage(body.stage)
    wanted = [p for p in ([body.profile_id] if body.profile_id else []) + body.profile_ids if p]
    if not wanted:
        raise HTTPException(status_code=400, detail="Provide profile_id or profile_ids")
    valid = set(db.scalars(
        select(Profile.profile_id).where(Profile.profile_id.in_(wanted))).all())
    already = set(db.scalars(
        select(TalentPoolMember.profile_id)
        .where(TalentPoolMember.pool_id == pool.pool_id,
               TalentPoolMember.profile_id.in_(wanted))).all())
    added = 0
    for pid in wanted:
        if pid not in valid or pid in already:
            continue
        db.add(TalentPoolMember(pool_id=pool.pool_id, profile_id=pid,
                                stage=body.stage, note=body.note,
                                added_by_user_id=user.user_id))
        already.add(pid)
        added += 1
    pool.updated_at = utcnow()
    db.commit()
    return {"added": added, "skipped": len(wanted) - added,
            "missing": sorted(set(wanted) - valid),
            "pool": _pool_json(pool, _counts(db, [pool.pool_id]))}


@router.patch("/{pool_id}/members/{profile_id}")
def update_member(pool_id: str, profile_id: str, body: MemberUpdate,
                  user: CurrentUser, db: DbSession):
    pool = _pool_or_404(db, pool_id, user)
    member = db.scalar(
        select(TalentPoolMember).where(TalentPoolMember.pool_id == pool.pool_id,
                                       TalentPoolMember.profile_id == profile_id))
    if not member:
        raise HTTPException(status_code=404, detail="Candidate is not in this pool")
    if body.stage is not None:
        member.stage = _check_stage(body.stage)
    if body.note is not None:
        member.note = body.note
    member.updated_at = utcnow()
    pool.updated_at = utcnow()
    db.commit()
    return {"profile_id": profile_id, "stage": member.stage, "note": member.note}


@router.delete("/{pool_id}/members/{profile_id}", status_code=204)
def remove_member(pool_id: str, profile_id: str, user: CurrentUser, db: DbSession):
    pool = _pool_or_404(db, pool_id, user)
    db.execute(delete(TalentPoolMember).where(
        TalentPoolMember.pool_id == pool.pool_id,
        TalentPoolMember.profile_id == profile_id))
    pool.updated_at = utcnow()
    db.commit()


# --- Which pools already hold these candidates -----------------------------

@router.post("/membership")
def membership(body: dict, user: CurrentUser, db: DbSession):
    """Map profile_id -> [pool_id...] so the directory can show what is saved."""
    _require_provider_directory_access(user)
    ids = [p for p in (body.get("profile_ids") or []) if p][:200]
    if not ids:
        return {}
    rows = db.execute(
        select(TalentPoolMember.profile_id, TalentPoolMember.pool_id)
        .join(TalentPool, TalentPool.pool_id == TalentPoolMember.pool_id)
        .where(TalentPool.owner_user_id == user.user_id,
               TalentPoolMember.profile_id.in_(ids))
    ).all()
    out: dict[str, list[str]] = {}
    for pid, pool_id in rows:
        out.setdefault(pid, []).append(pool_id)
    return out


# --- Export ----------------------------------------------------------------

@router.get("/{pool_id}/export.csv")
def export_pool(pool_id: str, user: CurrentUser, db: DbSession):
    """CSV of the pool for outreach. Identity columns are filled only for
    candidates this recruiter has released — the rest export as masked."""
    pool = _pool_or_404(db, pool_id, user)
    rows = db.execute(
        select(TalentPoolMember, Profile)
        .join(Profile, Profile.profile_id == TalentPoolMember.profile_id)
        .options(defer(Profile.resume_sections), defer(Profile.education))
        .where(TalentPoolMember.pool_id == pool.pool_id)
        .order_by(TalentPoolMember.created_at.desc())
    ).all()
    released = _released_profile_ids(db, user, [p.profile_id for _, p in rows])

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "email", "phone", "license", "category", "specialty",
                "city", "state", "years_experience", "stage", "note", "added_at"])
    for member, p in rows:
        is_open = p.profile_id in released or bool(p.user_id and p.user_id == user.user_id)
        name = (" ".join(x for x in (p.first_name, p.last_name) if x).strip()
                if is_open else "[not released]")
        w.writerow([
            name,
            (p.email or "") if is_open else "",
            (p.phone or "") if is_open else "",
            p.profession_type or "", p.provider_category or "", p.specialty or "",
            p.city or "", p.state_code or "",
            p.years_experience if p.years_experience is not None else "",
            member.stage, (member.note or "").replace("\n", " "),
            member.created_at.strftime("%Y-%m-%d") if member.created_at else "",
        ])
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in pool.name)[:40] or "pool"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{safe}.csv"'},
    )
