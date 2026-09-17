"""Recruiter analytics: recruitment funnel + conversation/CRM table.

Backs the analytics view in healthboard-chat-platform.html.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, or_, select

from ..deps import CurrentUser, DbSession
from ..models import (
    Application,
    AuditLog,
    CreditAccount,
    Employer,
    EmployerMember,
    JobPosting,
    Message,
    MessageThread,
    Offer,
    Profile,
    User,
)
from ..models.enums import ApplicationStatus, JobStatus, OfferStatus
from ..database import utcnow
from ..services import org_roles

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def _employer_ids_for(db: DbSession, user: CurrentUser) -> list[str]:
    owned = db.scalars(
        select(Employer.employer_id).where(Employer.owner_user_id == user.user_id)
    ).all()
    member = db.scalars(
        select(EmployerMember.employer_id).where(EmployerMember.user_id == user.user_id)
    ).all()
    return list({*owned, *member})


@router.get("/medhunt")
def medhunt_extension_activity(
    user: CurrentUser,
    db: DbSession,
    days: int = Query(30, ge=1, le=365),
    employer_id: str | None = None,
):
    """Detailed MedHunt extension usage, scoped to the current organization.

    Owners, admins and managers can see their whole team. Regular members see
    only their own events. Returning raw event rows as well as aggregates makes
    the analytics page useful for both daily oversight and source attribution.
    """
    from .extension import MEDHUNT_ATTEMPT_ACTION, MEDHUNT_ENRICHED_ACTION

    member_ids = [user.user_id]
    scope = "personal"
    employer = None
    if employer_id:
        employer = db.get(Employer, employer_id)
        if not employer or org_roles.role_of(db, employer, user) is None:
            raise HTTPException(status_code=403, detail="Not a member of this organisation")
    else:
        employer_ids = _employer_ids_for(db, user)
        if employer_ids:
            employer = db.get(Employer, employer_ids[0])

    if employer:
        role = org_roles.role_of(db, employer, user)
        if org_roles.can(role, "analytics"):
            member_ids = list({
                employer.owner_user_id,
                *db.scalars(select(EmployerMember.user_id).where(
                    EmployerMember.employer_id == employer.employer_id
                )).all(),
            })
            scope = "organization"

    since = utcnow() - timedelta(days=days)
    events = db.scalars(
        select(AuditLog).where(
            AuditLog.actor_user_id.in_(member_ids),
            AuditLog.action.in_((MEDHUNT_ENRICHED_ACTION, MEDHUNT_ATTEMPT_ACTION)),
            AuditLog.created_at >= since,
        ).order_by(AuditLog.created_at.desc())
    ).all()

    users = {u.user_id: u for u in db.scalars(
        select(User).where(User.user_id.in_(member_ids))
    )}
    profiles = {p.user_id: p for p in db.scalars(
        select(Profile).where(Profile.user_id.in_(member_ids))
    )}
    per_member: dict[str, dict] = {}
    source_counts: Counter[str] = Counter()
    enriched_candidates: set[str] = set()

    for uid in member_ids:
        account = users.get(uid)
        profile = profiles.get(uid)
        name = (f"{profile.first_name} {profile.last_name}".strip() if profile else None)
        per_member[uid] = {
            "user_id": uid,
            "name": name,
            "email": account.email if account else None,
            "checks": 0,
            "enriched": 0,
            "candidates_enriched": set(),
            "last_used_at": None,
        }

    activity = []
    for event in events:
        meta = event.meta or {}
        uid = event.actor_user_id
        row = per_member.get(uid)
        if row is None:
            continue
        source = str(meta.get("source") or "Unknown").strip() or "Unknown"
        candidate_id = str(meta.get("candidate_id") or "").strip()
        enriched = event.action == MEDHUNT_ENRICHED_ACTION
        row["checks"] += 1
        if row["last_used_at"] is None:
            row["last_used_at"] = event.created_at
        if enriched:
            row["enriched"] += 1
            source_counts[source] += 1
            if candidate_id:
                row["candidates_enriched"].add(candidate_id)
                enriched_candidates.add(candidate_id)
        activity.append({
            "event_id": event.entity_id,
            "user_id": uid,
            "user_name": row["name"] or row["email"] or "Team member",
            "candidate_id": candidate_id or None,
            "source": source,
            "status": meta.get("status") or ("success" if enriched else "attempted"),
            "enriched": enriched,
            "used_at": event.created_at,
        })

    members = []
    for row in per_member.values():
        candidate_ids = row.pop("candidates_enriched")
        row["candidates_enriched"] = len(candidate_ids)
        if row["checks"]:
            members.append(row)
    members.sort(key=lambda item: (item["last_used_at"] is not None,
                                   item["last_used_at"]), reverse=True)
    return {
        "scope": scope,
        "organization": ({"employer_id": employer.employer_id, "org_name": employer.org_name}
                         if employer else None),
        "window_days": days,
        "summary": {
            "users": len(members),
            "checks": len(events),
            "enrichments": sum(1 for event in events
                               if event.action == MEDHUNT_ENRICHED_ACTION),
            "candidates_enriched": len(enriched_candidates),
        },
        "members": members,
        "sources": [{"source": source, "enriched": count}
                    for source, count in source_counts.most_common()],
        "activity": activity[:100],
    }


@router.get("/funnel")
def recruitment_funnel(user: CurrentUser, db: DbSession):
    """Counts of applications per ATS stage across the recruiter's jobs."""
    employer_ids = _employer_ids_for(db, user)
    if not employer_ids:
        return {"stages": {}, "total": 0}

    job_ids = db.scalars(
        select(JobPosting.job_id).where(JobPosting.employer_id.in_(employer_ids))
    ).all()
    stages = {}
    total = 0
    if job_ids:
        rows = db.execute(
            select(Application.status, func.count())
            .where(Application.job_id.in_(job_ids))
            .group_by(Application.status)
        ).all()
        for status_val, count in rows:
            name = status_val.value if hasattr(status_val, "value") else str(status_val)
            stages[name] = count
            total += count
    # Ensure every stage is represented.
    funnel = {s.value: stages.get(s.value, 0) for s in ApplicationStatus}
    return {"stages": funnel, "total": total}


@router.get("/kpis")
def kpis(user: CurrentUser, db: DbSession):
    """Top-line CRM KPIs for the recruiter dashboard."""
    employer_ids = _employer_ids_for(db, user)
    job_ids = (
        db.scalars(select(JobPosting.job_id).where(JobPosting.employer_id.in_(employer_ids))).all()
        if employer_ids else []
    )

    active_conversations = db.scalar(
        select(func.count()).select_from(MessageThread).where(
            (MessageThread.participant_a_id == user.user_id)
            | (MessageThread.participant_b_id == user.user_id)
        )
    ) or 0

    def _count_status(status_val):
        if not job_ids:
            return 0
        return db.scalar(
            select(func.count()).select_from(Application).where(
                Application.job_id.in_(job_ids), Application.status == status_val
            )
        ) or 0

    offers_out = db.scalar(
        select(func.count()).select_from(Offer).where(
            Offer.recruiter_user_id == user.user_id, Offer.status == OfferStatus.sent
        )
    ) or 0

    return {
        "active_conversations": active_conversations,
        "in_interview": _count_status(ApplicationStatus.interview),
        "offers_out": offers_out,
        "hired": _count_status(ApplicationStatus.hired),
        "rejected": _count_status(ApplicationStatus.rejected),
    }


@router.get("/conversations")
def conversation_table(user: CurrentUser, db: DbSession):
    """Per-thread CRM rows: counterpart, ATS stage, message counts, last activity."""
    threads = db.scalars(
        select(MessageThread).where(
            (MessageThread.participant_a_id == user.user_id)
            | (MessageThread.participant_b_id == user.user_id)
        ).order_by(MessageThread.last_message_at.desc().nullslast())
    ).all()

    rows = []
    for t in threads:
        other_id = (t.participant_b_id if t.participant_a_id == user.user_id
                    else t.participant_a_id)
        counterpart = db.scalar(select(Profile).where(Profile.user_id == other_id))
        # Friendly name when the other party has no candidate profile (e.g. a
        # recruiter): use their organisation name, else their email handle.
        if counterpart:
            other_name = f"{counterpart.first_name} {counterpart.last_name}"
        else:
            emp = db.scalar(select(Employer).where(Employer.owner_user_id == other_id))
            other_user = db.get(User, other_id)
            other_name = (emp.org_name if emp else
                          (other_user.email.split("@")[0].replace(".", " ").title()
                           if other_user else other_id))
        sent = db.scalar(
            select(func.count()).select_from(Message).where(
                Message.thread_id == t.thread_id, Message.sender_id == user.user_id
            )
        ) or 0
        received = db.scalar(
            select(func.count()).select_from(Message).where(
                Message.thread_id == t.thread_id, Message.recipient_id == user.user_id
            )
        ) or 0
        response_rate = round(received / sent, 2) if sent else 0.0
        rows.append({
            "thread_id": t.thread_id,
            "candidate": other_name,
            "ats_stage": t.ats_stage,
            "messages_sent": sent,
            "messages_received": received,
            "response_rate": response_rate,
            "last_message_at": t.last_message_at,
        })
    return {"conversations": rows, "total": len(rows)}


# --- Sourcing analytics ---------------------------------------------------
# The funnel above measures inbound applications, which a sourcing-led agency
# has none of. What a recruiter here actually does is reveal contacts, build
# pools, run match jobs and message people — so measure that instead.

@router.get("/sourcing")
def sourcing_activity(user: CurrentUser, db: DbSession, days: int = 30):
    from datetime import timedelta

    from ..database import utcnow
    from ..models import (
        AuditLog,
        MatchRun,
        Notification,
        SavedSearch,
        TalentPool,
        TalentPoolMember,
    )
    from .profiles import RELEASE_ACTION
    from .extension import MEDHUNT_ATTEMPT_ACTION, MEDHUNT_ENRICHED_ACTION

    since = utcnow() - timedelta(days=max(1, days))
    uid = user.user_id

    def count(stmt) -> int:
        return db.scalar(stmt) or 0

    releases = count(select(func.count()).select_from(AuditLog)
                     .where(AuditLog.actor_user_id == uid, AuditLog.action == RELEASE_ACTION))
    releases_recent = count(select(func.count()).select_from(AuditLog)
                            .where(AuditLog.actor_user_id == uid,
                                   AuditLog.action == RELEASE_ACTION,
                                   AuditLog.created_at >= since))
    medhunt_enriched = count(select(func.count()).select_from(AuditLog).where(
        AuditLog.actor_user_id == uid,
        AuditLog.action == MEDHUNT_ENRICHED_ACTION,
    ))
    medhunt_enriched_recent = count(select(func.count()).select_from(AuditLog).where(
        AuditLog.actor_user_id == uid,
        AuditLog.action == MEDHUNT_ENRICHED_ACTION,
        AuditLog.created_at >= since,
    ))
    medhunt_attempts_recent = count(select(func.count()).select_from(AuditLog).where(
        AuditLog.actor_user_id == uid,
        AuditLog.action.in_((MEDHUNT_ENRICHED_ACTION, MEDHUNT_ATTEMPT_ACTION)),
        AuditLog.created_at >= since,
    ))
    pool_ids = db.scalars(select(TalentPool.pool_id)
                          .where(TalentPool.owner_user_id == uid)).all()
    shortlisted = count(select(func.count()).select_from(TalentPoolMember)
                        .where(TalentPoolMember.pool_id.in_(pool_ids))) if pool_ids else 0
    by_stage = dict(db.execute(
        select(TalentPoolMember.stage, func.count())
        .where(TalentPoolMember.pool_id.in_(pool_ids))
        .group_by(TalentPoolMember.stage)).all()) if pool_ids else {}

    runs = db.scalars(select(MatchRun).where(MatchRun.requested_by_user_id == uid)).all()
    ranked = sum(r.candidate_count or 0 for r in runs)
    avg_score = (round(sum(float(r.avg_score or 0) for r in runs) / len(runs), 1)
                 if runs else 0.0)

    sent = count(select(func.count()).select_from(Message).where(Message.sender_id == uid))
    received = count(select(func.count()).select_from(Message).where(Message.recipient_id == uid))
    threads = count(select(func.count()).select_from(MessageThread).where(
        (MessageThread.participant_a_id == uid) | (MessageThread.participant_b_id == uid)))

    listable = count(select(func.count()).select_from(Profile)
                     .where(Profile.is_listable.is_(True)))
    reachable = count(select(func.count()).select_from(Profile).where(
        Profile.is_listable.is_(True),
        # trim() (not Postgres-only btrim) so this works on SQLite dev too.
        ((Profile.email.isnot(None)) & (func.length(func.trim(Profile.email)) > 0))
        | ((Profile.phone.isnot(None)) & (func.length(func.trim(Profile.phone)) > 0))))

    # How much of the shortlist actually got worked, and how far it got.
    moved = sum(n for s, n in by_stage.items() if s != "sourced")
    return {
        "window_days": days,
        "directory": {
            "listable": listable,
            "reachable": reachable,
            "reachable_pct": round(100 * reachable / listable, 1) if listable else 0.0,
        },
        "contacts": {"released_total": releases, "released_recent": releases_recent},
        "medhunt": {
            "enriched_total": medhunt_enriched,
            "enriched_recent": medhunt_enriched_recent,
            "attempts_recent": medhunt_attempts_recent,
        },
        "pools": {
            "pools": len(pool_ids),
            "shortlisted": shortlisted,
            "by_stage": by_stage,
            "worked": moved,
            "worked_pct": round(100 * moved / shortlisted, 1) if shortlisted else 0.0,
        },
        "sourcing_runs": {
            "runs": len(runs), "candidates_ranked": ranked, "avg_match_score": avg_score,
        },
        "messaging": {"threads": threads, "sent": sent, "received": received},
        "saved_searches": count(select(func.count()).select_from(SavedSearch)
                                .where(SavedSearch.owner_user_id == uid)),
        "notifications": count(select(func.count()).select_from(Notification)
                               .where(Notification.user_id == uid)),
    }


# Real US state codes — the imported directory carries junk state values, so a
# plain distinct count over-reports; constrain to the 50 states + DC.
_US_STATES = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
)


@router.get("/market")
def market(user: CurrentUser, db: DbSession):
    """Marketplace supply & demand — the real, populated data every recruiter can
    use: the size and shape of the talent directory (supply) and the open roles
    on the board (demand). Platform-wide figures plus this recruiter's credits.
    """
    def top(rows):
        return [{"label": str(k), "count": int(c)} for k, c in rows if k]

    listable = db.scalar(select(func.count()).select_from(Profile)
                         .where(Profile.is_listable.is_(True))) or 0
    reachable = db.scalar(select(func.count()).select_from(Profile).where(
        Profile.is_listable.is_(True),
        # trim() (not btrim) so this also works on SQLite in dev.
        or_((Profile.email.isnot(None)) & (func.length(func.trim(Profile.email)) > 0),
            (Profile.phone.isnot(None)) & (func.length(func.trim(Profile.phone)) > 0)))) or 0
    states = db.scalar(
        select(func.count(func.distinct(func.upper(Profile.state_code))))
        .where(Profile.is_listable.is_(True),
               func.upper(Profile.state_code).in_(_US_STATES))) or 0
    jobs_active = db.scalar(select(func.count()).select_from(JobPosting)
                            .where(JobPosting.status == JobStatus.active)) or 0

    supply = top(db.execute(
        select(Profile.profession_type, func.count())
        .where(Profile.is_listable.is_(True),
               Profile.profession_type.isnot(None),
               func.length(func.trim(Profile.profession_type)) > 0)
        .group_by(Profile.profession_type)
        .order_by(func.count().desc()).limit(7)).all())
    demand_specialty = top(db.execute(
        select(JobPosting.specialty, func.count())
        .where(JobPosting.status == JobStatus.active, JobPosting.specialty.isnot(None))
        .group_by(JobPosting.specialty)
        .order_by(func.count().desc()).limit(7)).all())
    demand_state = top(db.execute(
        select(JobPosting.state_code, func.count())
        .where(JobPosting.status == JobStatus.active, JobPosting.state_code.isnot(None))
        .group_by(JobPosting.state_code)
        .order_by(func.count().desc()).limit(8)).all())

    acct = db.scalar(select(CreditAccount).where(CreditAccount.user_id == user.user_id))
    return {
        "providers": {
            "listable": listable,
            "reachable": reachable,
            "reachable_pct": round(100 * reachable / listable, 1) if listable else 0.0,
            "states": states,
        },
        "jobs_active": jobs_active,
        "supply": supply,
        "demand_specialty": demand_specialty,
        "demand_state": demand_state,
        "credits": {
            "balance": acct.balance if acct else 0,
            "spent": acct.lifetime_spent if acct else 0,
        },
    }
