"""Client facilities — the agency's book of business.

A Client is a hospital/facility the team places candidates into. It is scoped to
the agency (the recruiter plus their team, the same rule submissions use), so a
colleague covering a desk sees the same clients. Submissions point at a client,
which is how the free-text ``facility`` field becomes a real relationship.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select

from ..database import utcnow
from ..deps import CurrentUser, DbSession
from ..models import Client, Employer, Profile, Submission
from .profiles import (
    _masked_name,
    _released_profile_ids,
    _require_provider_directory_access,
)
from .submissions import _team_user_ids

router = APIRouter(prefix="/api/clients", tags=["clients"])


class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    facility_type: Optional[str] = Field(default=None, max_length=80)
    city: Optional[str] = Field(default=None, max_length=120)
    state_code: Optional[str] = Field(default=None, max_length=2)
    website_url: Optional[str] = Field(default=None, max_length=255)
    contact_name: Optional[str] = Field(default=None, max_length=120)
    contact_email: Optional[EmailStr] = None
    contact_phone: Optional[str] = Field(default=None, max_length=40)
    default_bill_rate: Optional[float] = None
    notes: Optional[str] = None


class ClientUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    facility_type: Optional[str] = None
    city: Optional[str] = None
    state_code: Optional[str] = None
    website_url: Optional[str] = None
    contact_name: Optional[str] = None
    contact_email: Optional[EmailStr] = None
    contact_phone: Optional[str] = None
    default_bill_rate: Optional[float] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None


def _employer_id_for(db: DbSession, user: CurrentUser) -> Optional[str]:
    return db.scalar(select(Employer.employer_id)
                     .where(Employer.owner_user_id == user.user_id))


def _own_client(db: DbSession, client_id: str, user: CurrentUser) -> Client:
    client = db.get(Client, client_id)
    if not client or client.owner_user_id not in _team_user_ids(db, user):
        raise HTTPException(status_code=404, detail="Client not found")
    return client


def _json(c: Client, subs: int = 0, placed: int = 0) -> dict:
    return {
        "client_id": c.client_id,
        "name": c.name,
        "facility_type": c.facility_type,
        "city": c.city,
        "state_code": c.state_code,
        "location": ", ".join(x for x in [c.city, c.state_code] if x) or None,
        "website_url": c.website_url,
        "contact_name": c.contact_name,
        "contact_email": c.contact_email,
        "contact_phone": c.contact_phone,
        "default_bill_rate": float(c.default_bill_rate) if c.default_bill_rate is not None else None,
        "notes": c.notes,
        "is_active": c.is_active,
        "submissions": subs,
        "placed": placed,
        "created_at": c.created_at,
    }


@router.get("")
def list_clients(user: CurrentUser, db: DbSession,
                 q: Optional[str] = Query(None),
                 limit: int = Query(200, ge=1, le=500)):
    _require_provider_directory_access(user)
    team = _team_user_ids(db, user)
    stmt = select(Client).where(Client.owner_user_id.in_(team))
    if q:
        stmt = stmt.where(Client.name.ilike(f"%{q}%"))
    rows = db.scalars(stmt.order_by(Client.is_active.desc(), Client.name.asc())
                      .limit(limit)).all()

    # Submission counts per client (total + placed), in two grouped queries.
    ids = [c.client_id for c in rows]
    subs_by = dict(db.execute(
        select(Submission.client_id, func.count())
        .where(Submission.client_id.in_(ids)).group_by(Submission.client_id)).all()) if ids else {}
    placed_by = dict(db.execute(
        select(Submission.client_id, func.count())
        .where(Submission.client_id.in_(ids), Submission.status == "placed")
        .group_by(Submission.client_id)).all()) if ids else {}

    items = [_json(c, subs_by.get(c.client_id, 0), placed_by.get(c.client_id, 0)) for c in rows]
    return {"items": items, "total": len(items)}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_client(body: ClientIn, user: CurrentUser, db: DbSession):
    _require_provider_directory_access(user)
    data = body.model_dump()
    if data.get("state_code"):
        data["state_code"] = data["state_code"].upper()
    client = Client(owner_user_id=user.user_id,
                    employer_id=_employer_id_for(db, user), **data)
    db.add(client)
    db.commit()
    db.refresh(client)
    return _json(client)


@router.get("/{client_id}")
def get_client(client_id: str, user: CurrentUser, db: DbSession):
    client = _own_client(db, client_id, user)
    subs = db.scalars(select(Submission).where(Submission.client_id == client_id)
                      .order_by(Submission.submitted_at.desc()).limit(100)).all()
    profs = {p.profile_id: p for p in db.scalars(
        select(Profile).where(Profile.profile_id.in_([s.profile_id for s in subs])))} if subs else {}
    released = _released_profile_ids(db, user, [s.profile_id for s in subs])
    counts: dict[str, int] = {}
    submissions = []
    for s in subs:
        counts[s.status] = counts.get(s.status, 0) + 1
        p = profs.get(s.profile_id)
        is_open = s.profile_id in released
        submissions.append({
            "submission_id": s.submission_id,
            "profile_id": s.profile_id,
            "candidate": (f"{p.first_name or ''} {p.last_name or ''}".strip()
                          if (p and is_open) else (_masked_name(p) if p else "Unknown")),
            "status": s.status,
            "bill_rate": float(s.bill_rate) if s.bill_rate is not None else None,
            "pay_rate": float(s.pay_rate) if s.pay_rate is not None else None,
            "margin": (float(s.bill_rate) - float(s.pay_rate))
                      if (s.bill_rate is not None and s.pay_rate is not None) else None,
            "submitted_at": s.submitted_at,
        })
    return {"client": _json(client, len(subs), counts.get("placed", 0)),
            "submissions": submissions, "by_status": counts}


@router.patch("/{client_id}")
def update_client(client_id: str, body: ClientUpdate, user: CurrentUser, db: DbSession):
    client = _own_client(db, client_id, user)
    for field, value in body.model_dump(exclude_unset=True).items():
        if field == "state_code" and value:
            value = value.upper()
        setattr(client, field, value)
    client.updated_at = utcnow()
    db.commit()
    db.refresh(client)
    return _json(client)


@router.delete("/{client_id}", status_code=204)
def delete_client(client_id: str, user: CurrentUser, db: DbSession):
    client = _own_client(db, client_id, user)
    # Submissions keep their denormalised `facility`; the FK is SET NULL.
    db.delete(client)
    db.commit()
