"""Recruiter outreach: templates, campaigns, sending and tracking.

Three rules are enforced here rather than in the UI, because they are the ones
that matter if this is ever pointed at 85,000 real people:

1. **Release before contact.** A recruiter may only email a candidate whose
   contact details they have already released through the audited flow. Bulk
   email must not become a side door around the masking system.
2. **Suppression is absolute.** Anyone who opted out, at any time, for any
   campaign, is skipped — checked per address at send time, not per campaign.
3. **Every send is opt-out-able.** An unsubscribe link is appended to every
   message; there is no flag to turn it off.

Without mail credentials configured the whole flow still runs and records
`sent`, marking each send simulated — so the pipeline is testable before a
domain is verified.
"""
from __future__ import annotations

import re
import secrets
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..config import settings
from ..database import utcnow
from ..deps import CurrentUser, DbSession
from ..models import (
    MERGE_FIELDS,
    AuditLog,
    EmailTemplate,
    OutreachCampaign,
    OutreachMessage,
    Profile,
    Suppression,
    TalentPool,
    TalentPoolMember,
)
from ..services.email import send_email
from .profiles import RELEASE_ACTION, _require_provider_directory_access

router = APIRouter(prefix="/api/outreach", tags=["outreach"])

# A 1x1 transparent GIF served by the open-tracking endpoint.
_PIXEL = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c000000000100"
    "010000020144003b"
)
_MERGE_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


# --- Schemas ---------------------------------------------------------------

class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1)


class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    pool_id: str
    template_id: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None


# --- Merge-field rendering -------------------------------------------------

def _context(p: Profile) -> dict:
    full = " ".join(x for x in (p.first_name, p.last_name) if x).strip()
    return {
        "first_name": (p.first_name or "there").strip(),
        "last_name": (p.last_name or "").strip(),
        "full_name": full or "there",
        "specialty": p.specialty or "your specialty",
        "profession_type": p.profession_type or "your field",
        "city": p.city or "your area",
        "state_code": p.state_code or "",
        "years_experience": str(p.years_experience or ""),
    }


def render(text_: str, p: Profile) -> str:
    ctx = _context(p)
    return _MERGE_RE.sub(lambda m: ctx.get(m.group(1).lower(), m.group(0)), text_ or "")


def _released_ids(db: DbSession, user: CurrentUser) -> set[str]:
    return {r for r in db.scalars(
        select(AuditLog.entity_id).where(AuditLog.actor_user_id == user.user_id,
                                         AuditLog.action == RELEASE_ACTION)).all() if r}


def _suppressed(db: DbSession) -> set[str]:
    return {e.lower() for e in db.scalars(select(Suppression.email)).all() if e}


def _own_campaign(db: DbSession, cid: str, user: CurrentUser) -> OutreachCampaign:
    c = db.get(OutreachCampaign, cid)
    if not c:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if c.owner_user_id != user.user_id and user.role.value != "admin":
        raise HTTPException(status_code=403, detail="This campaign belongs to another recruiter")
    return c


# --- Templates -------------------------------------------------------------

@router.get("/templates")
def list_templates(user: CurrentUser, db: DbSession):
    _require_provider_directory_access(user)
    rows = db.scalars(select(EmailTemplate)
                      .where(EmailTemplate.owner_user_id == user.user_id)
                      .order_by(EmailTemplate.updated_at.desc())).all()
    return {"items": [{"template_id": t.template_id, "name": t.name,
                       "subject": t.subject, "body": t.body} for t in rows],
            "merge_fields": list(MERGE_FIELDS)}


@router.post("/templates", status_code=201)
def create_template(body: TemplateIn, user: CurrentUser, db: DbSession):
    _require_provider_directory_access(user)
    if db.scalar(select(EmailTemplate).where(
            EmailTemplate.owner_user_id == user.user_id,
            func.lower(EmailTemplate.name) == body.name.strip().lower())):
        raise HTTPException(status_code=409, detail="You already have a template with that name")
    t = EmailTemplate(owner_user_id=user.user_id, name=body.name.strip(),
                      subject=body.subject, body=body.body)
    db.add(t)
    db.commit()
    db.refresh(t)
    return {"template_id": t.template_id, "name": t.name}


@router.delete("/templates/{template_id}", status_code=204)
def delete_template(template_id: str, user: CurrentUser, db: DbSession):
    t = db.get(EmailTemplate, template_id)
    if not t or (t.owner_user_id != user.user_id and user.role.value != "admin"):
        raise HTTPException(status_code=404, detail="Template not found")
    db.delete(t)
    db.commit()


# --- Campaigns -------------------------------------------------------------

@router.get("/campaigns")
def list_campaigns(user: CurrentUser, db: DbSession):
    _require_provider_directory_access(user)
    rows = db.scalars(select(OutreachCampaign)
                      .where(OutreachCampaign.owner_user_id == user.user_id)
                      .order_by(OutreachCampaign.created_at.desc())).all()
    return {"items": [{
        "campaign_id": c.campaign_id, "name": c.name, "status": c.status,
        "pool_id": c.pool_id, "subject": c.subject,
        "total": c.total, "sent": c.sent, "skipped": c.skipped, "failed": c.failed,
        "opened": c.opened, "replied": c.replied,
        "open_rate": round(100 * c.opened / c.sent, 1) if c.sent else 0.0,
        "created_at": c.created_at,
    } for c in rows], "email_configured": bool(settings.email_enabled and settings.sendgrid_api_key)}


@router.post("/campaigns", status_code=201)
def create_campaign(body: CampaignIn, user: CurrentUser, db: DbSession):
    """Build a campaign from a talent pool. Recipients are resolved now so the
    recruiter can see exactly who is reachable before anything is sent."""
    _require_provider_directory_access(user)
    pool = db.get(TalentPool, body.pool_id)
    if not pool or pool.owner_user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Pool not found")

    subject, text_body = body.subject, body.body
    if body.template_id:
        t = db.get(EmailTemplate, body.template_id)
        if not t or t.owner_user_id != user.user_id:
            raise HTTPException(status_code=404, detail="Template not found")
        subject = subject or t.subject
        text_body = text_body or t.body
    if not subject or not text_body:
        raise HTTPException(status_code=400, detail="Provide a template_id, or a subject and body")

    campaign = OutreachCampaign(owner_user_id=user.user_id, name=body.name.strip(),
                                pool_id=pool.pool_id, template_id=body.template_id,
                                subject=subject, body=text_body)
    db.add(campaign)
    db.flush()

    released = _released_ids(db, user)
    suppressed = _suppressed(db)
    rows = db.execute(
        select(TalentPoolMember, Profile)
        .join(Profile, Profile.profile_id == TalentPoolMember.profile_id)
        .where(TalentPoolMember.pool_id == pool.pool_id)
    ).all()

    counts = {"queued": 0, "skipped": 0}
    for _member, p in rows:
        email = (p.email or "").strip()
        reason = None
        if p.profile_id not in released:
            reason = "contact not released"
        elif not email:
            reason = "no email address"
        elif email.lower() in suppressed:
            reason = "unsubscribed"
        status = "skipped" if reason else "queued"
        counts["skipped" if reason else "queued"] += 1
        db.add(OutreachMessage(
            campaign_id=campaign.campaign_id, profile_id=p.profile_id,
            to_email=email or None, status=status, reason=reason,
            token=secrets.token_urlsafe(24),
            subject=render(subject, p), body=render(text_body, p),
        ))

    campaign.total = len(rows)
    campaign.skipped = counts["skipped"]
    db.commit()
    return {"campaign_id": campaign.campaign_id, "total": campaign.total,
            "ready_to_send": counts["queued"], "skipped": counts["skipped"]}


@router.get("/campaigns/{campaign_id}")
def campaign_detail(campaign_id: str, user: CurrentUser, db: DbSession,
                    limit: int = Query(50, ge=1, le=200)):
    c = _own_campaign(db, campaign_id, user)
    msgs = db.scalars(select(OutreachMessage)
                      .where(OutreachMessage.campaign_id == c.campaign_id)
                      .order_by(OutreachMessage.status, OutreachMessage.created_at)
                      .limit(limit)).all()
    by_reason = dict(db.execute(
        select(OutreachMessage.reason, func.count())
        .where(OutreachMessage.campaign_id == c.campaign_id,
               OutreachMessage.reason.isnot(None))
        .group_by(OutreachMessage.reason)).all())
    return {
        "campaign_id": c.campaign_id, "name": c.name, "status": c.status,
        "subject": c.subject, "body": c.body,
        "total": c.total, "sent": c.sent, "skipped": c.skipped,
        "failed": c.failed, "opened": c.opened, "replied": c.replied,
        "skip_reasons": by_reason,
        "messages": [{
            "message_id": m.message_id, "status": m.status, "reason": m.reason,
            "to_email": m.to_email, "subject": m.subject,
            "opened_at": m.opened_at, "sent_at": m.sent_at,
        } for m in msgs],
    }


@router.post("/campaigns/{campaign_id}/send")
def send_campaign(campaign_id: str, user: CurrentUser, db: DbSession,
                  limit: int = Query(500, ge=1, le=2000)):
    """Send everything still queued. Suppression is re-checked per address."""
    c = _own_campaign(db, campaign_id, user)
    suppressed = _suppressed(db)
    queued = db.scalars(select(OutreachMessage).where(
        OutreachMessage.campaign_id == c.campaign_id,
        OutreachMessage.status == "queued").limit(limit)).all()

    base = settings.frontend_base_url.rstrip("/")
    simulated = not (settings.email_enabled and settings.sendgrid_api_key)
    sent = failed = skipped = 0
    # Sending costs no credits. The credit was already spent to reveal this
    # candidate's contact details; charging again to use them would bill the
    # recruiter twice for the same person.
    for m in queued:
        addr = (m.to_email or "").strip()
        if not addr or addr.lower() in suppressed:
            m.status, m.reason = "skipped", "unsubscribed"
            skipped += 1
            continue
        html = (
            f"<div style='font-family:system-ui,sans-serif;font-size:14px;line-height:1.6'>"
            f"{(m.body or '').replace(chr(10), '<br>')}"
            f"<hr style='border:0;border-top:1px solid #e5e7eb;margin:22px 0 10px'>"
            f"<p style='font-size:11px;color:#6b7280'>"
            f"You received this because a recruiter found your résumé. "
            f"<a href='{base}/api/outreach/unsubscribe/{m.token}'>Unsubscribe</a>.</p>"
            f"<img src='{base}/api/outreach/open/{m.token}' width='1' height='1' alt=''>"
            f"</div>"
        )
        ok = send_email(addr, m.subject or c.subject, html)
        # With no provider configured send_email returns False by design; the
        # row is still marked sent so the pipeline is exercisable end to end.
        m.status = "sent"
        m.sent_at = utcnow()
        if not ok and not simulated:
            m.status, m.reason = "failed", "provider rejected"
            failed += 1
        else:
            sent += 1

    c.sent += sent
    c.failed += failed
    c.skipped += skipped
    c.status = "sent" if not db.scalar(
        select(func.count()).select_from(OutreachMessage).where(
            OutreachMessage.campaign_id == c.campaign_id,
            OutreachMessage.status == "queued")) else "sending"
    c.updated_at = utcnow()
    db.commit()
    return {"sent": sent, "failed": failed, "skipped": skipped,
            "simulated": simulated, "status": c.status,
            "note": ("No mail provider configured — messages were recorded but not "
                     "delivered. Set email_enabled and sendgrid_api_key to send for real."
                     if simulated else None)}


# --- Tracking (public, no auth) --------------------------------------------

@router.get("/open/{token}", include_in_schema=False)
def track_open(token: str, db: DbSession):
    m = db.scalar(select(OutreachMessage).where(OutreachMessage.token == token))
    if m and not m.opened_at:
        m.opened_at = utcnow()
        if m.status == "sent":
            m.status = "opened"
        c = db.get(OutreachCampaign, m.campaign_id)
        if c:
            c.opened += 1
        db.commit()
    return Response(content=_PIXEL, media_type="image/gif",
                    headers={"Cache-Control": "no-store"})


@router.get("/unsubscribe/{token}", include_in_schema=False)
def unsubscribe(token: str, db: DbSession):
    m = db.scalar(select(OutreachMessage).where(OutreachMessage.token == token))
    if not m:
        return HTMLResponse("<p>That link is no longer valid.</p>", status_code=404)
    addr = (m.to_email or "").strip()
    if addr and not db.scalar(select(Suppression).where(
            func.lower(Suppression.email) == addr.lower())):
        db.add(Suppression(email=addr, reason="unsubscribed"))
    m.status = "unsubscribed"
    db.commit()
    return HTMLResponse(
        "<div style='font-family:system-ui,sans-serif;max-width:520px;margin:60px auto'>"
        "<h2>You're unsubscribed</h2>"
        "<p>We won't email you about roles again. You can close this page.</p></div>")


@router.post("/campaigns/{campaign_id}/messages/{message_id}/replied")
def mark_replied(campaign_id: str, message_id: str, user: CurrentUser, db: DbSession):
    """Record a reply. A provider webhook can call this once one is wired up."""
    c = _own_campaign(db, campaign_id, user)
    m = db.get(OutreachMessage, message_id)
    if not m or m.campaign_id != c.campaign_id:
        raise HTTPException(status_code=404, detail="Message not found")
    if not m.replied_at:
        m.replied_at = utcnow()
        m.status = "replied"
        c.replied += 1
        db.commit()
    return {"message_id": message_id, "status": m.status}


@router.get("/suppressions")
def list_suppressions(user: CurrentUser, db: DbSession):
    _require_provider_directory_access(user)
    rows = db.scalars(select(Suppression).order_by(Suppression.created_at.desc())
                      .limit(200)).all()
    return {"items": [{"email": s.email, "reason": s.reason,
                       "created_at": s.created_at} for s in rows],
            "total": db.scalar(select(func.count()).select_from(Suppression)) or 0}
