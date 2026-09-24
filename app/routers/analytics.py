"""Recruiter analytics: recruitment funnel + conversation/CRM table.

Backs the analytics view in healthboard-chat-platform.html.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone

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
    member_id: str | None = None,
    source: str | None = None,
    platform: str | None = None,
    provider: str | None = None,
    outcome: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = 1,
    page_size: int = 50,
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

    since = (datetime.combine(date_from, datetime.min.time(), timezone.utc)
             if date_from else
             datetime.combine(date_to - timedelta(days=days - 1), datetime.min.time(), timezone.utc)
             if date_to else utcnow() - timedelta(days=days))
    until = (datetime.combine(date_to + timedelta(days=1), datetime.min.time(), timezone.utc)
             if date_to else None)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="Start date must be on or before end date")
    if date_from and date_to and (date_to - date_from).days > 366:
        raise HTTPException(status_code=422, detail="Choose a date range of at most 366 days")
    event_window = [AuditLog.created_at >= since]
    if until:
        event_window.append(AuditLog.created_at < until)
    events = db.scalars(
        select(AuditLog).where(
            AuditLog.actor_user_id.in_(member_ids),
            AuditLog.action.in_((MEDHUNT_ENRICHED_ACTION, MEDHUNT_ATTEMPT_ACTION)),
            *event_window,
        ).order_by(AuditLog.created_at.desc())
    ).all()
    sms_events = db.scalars(
        select(AuditLog).where(
            AuditLog.actor_user_id.in_(member_ids),
            AuditLog.action.in_((
                "medhunt_sms_received", "medhunt_sms_sent",
                "medhunt_conversation_assigned",
            )),
            *event_window,
        ).order_by(AuditLog.created_at.desc())
    ).all()

    # Keep filter choices from the full authorized window. A selected filter
    # should not make the other choices disappear from the controls.
    available_platforms = sorted({
        str((event.meta or {}).get("platform") or
            (event.meta or {}).get("source") or "Unknown").strip() or "Unknown"
        for event in events
    }, key=str.casefold)
    available_providers = sorted({
        str((event.meta or {}).get("provider") or "Unknown").strip() or "Unknown"
        for event in events
    }, key=str.casefold)
    if member_id and member_id not in member_ids:
        raise HTTPException(status_code=403, detail="Not a member of this organisation")
    if outcome and outcome not in {"enriched", "attempted"}:
        raise HTTPException(status_code=422, detail="Invalid outcome")
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(status_code=422, detail="Invalid activity page")
    if member_id:
        events = [event for event in events if event.actor_user_id == member_id]
        sms_events = [event for event in sms_events if event.actor_user_id == member_id]
    selected_platform = platform or source
    if selected_platform:
        events = [event for event in events if (
            str((event.meta or {}).get("platform") or
                (event.meta or {}).get("source") or "Unknown").strip() or "Unknown"
        ).casefold() == selected_platform.casefold()]
    if provider:
        events = [event for event in events if (
            str((event.meta or {}).get("provider") or "Unknown").strip() or "Unknown"
        ).casefold() == provider.casefold()]
    if outcome:
        action = MEDHUNT_ENRICHED_ACTION if outcome == "enriched" else MEDHUNT_ATTEMPT_ACTION
        events = [event for event in events if event.action == action]

    users = {u.user_id: u for u in db.scalars(
        select(User).where(User.user_id.in_(member_ids))
    )}
    profiles = {p.user_id: p for p in db.scalars(
        select(Profile).where(Profile.user_id.in_(member_ids))
    )}
    member_options = [{
        "user_id": uid,
        "name": (f"{profiles[uid].first_name} {profiles[uid].last_name}".strip()
                 if uid in profiles else None),
        "email": users[uid].email if uid in users else None,
    } for uid in member_ids]
    member_options.sort(key=lambda row: (row["name"] or row["email"] or "").casefold())
    per_member: dict[str, dict] = {}
    source_counts: Counter[str] = Counter()
    source_checks: Counter[str] = Counter()
    enriched_candidates: set[str] = set()
    daily: dict[str, dict] = {}

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
        event_source = str(meta.get("platform") or meta.get("source") or "Unknown").strip() or "Unknown"
        candidate_id = str(meta.get("candidate_id") or "").strip()
        enriched = event.action == MEDHUNT_ENRICHED_ACTION
        row["checks"] += 1
        source_checks[event_source] += 1
        if row["last_used_at"] is None:
            row["last_used_at"] = event.created_at
        if enriched:
            row["enriched"] += 1
            source_counts[event_source] += 1
            if candidate_id:
                row["candidates_enriched"].add(candidate_id)
                enriched_candidates.add(candidate_id)
        day = event.created_at.date().isoformat()
        daily_row = daily.setdefault(day, {
            "date": day, "checks": 0, "enrichments": 0,
            "users": set(), "candidates_enriched": set(),
        })
        daily_row["checks"] += 1
        daily_row["users"].add(uid)
        if enriched:
            daily_row["enrichments"] += 1
            if candidate_id:
                daily_row["candidates_enriched"].add(candidate_id)
        activity.append({
            "event_id": event.entity_id,
            "user_id": uid,
            "user_name": row["name"] or row["email"] or "Team member",
            "user_email": row["email"],
            "candidate_id": candidate_id or None,
            "run_id": meta.get("run_id") or None,
            "source": event_source,
            "platform": event_source,
            "provider": meta.get("provider") or None,
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
    page_activity = activity[(page - 1) * page_size:page * page_size]
    candidate_ids = [row["candidate_id"] for row in page_activity if row["candidate_id"]]
    candidate_names = {profile.profile_id: f"{profile.first_name} {profile.last_name}".strip()
                       for profile in db.scalars(select(Profile).where(
                           Profile.profile_id.in_(candidate_ids)
                       ))} if candidate_ids else {}
    for row in page_activity:
        row["candidate_name"] = candidate_names.get(row["candidate_id"])
    return {
        "scope": scope,
        "organization": ({"employer_id": employer.employer_id, "org_name": employer.org_name}
                         if employer else None),
        "window_days": days,
        "filters": {"member_id": member_id, "source": source,
                    "platform": selected_platform, "provider": provider,
                    "outcome": outcome,
                    "date_from": date_from.isoformat() if date_from else None,
                    "date_to": date_to.isoformat() if date_to else None},
        "source_options": available_platforms,
        "platform_options": available_platforms,
        "provider_options": available_providers,
        "member_options": member_options,
        "summary": {
            "users": len(members),
            "checks": len(events),
            "enrichments": sum(1 for event in events
                               if event.action == MEDHUNT_ENRICHED_ACTION),
            "candidates_enriched": len(enriched_candidates),
            "sms_sent": sum(1 for event in sms_events if event.action == "medhunt_sms_sent"),
            "sms_replies": sum(1 for event in sms_events if event.action == "medhunt_sms_received"),
            "sms_assignments": sum(1 for event in sms_events if event.action == "medhunt_conversation_assigned"),
        },
        "members": members,
        "sources": [{
            "source": item, "checks": count,
            "enriched": source_counts[item],
        } for item, count in source_checks.most_common()],
        "daily": [{
            **row,
            "users": len(row["users"]),
            "candidates_enriched": len(row["candidates_enriched"]),
        } for _, row in sorted(daily.items(), reverse=True)],
        "activity_total": len(activity),
        "activity_page": page,
        "activity_page_size": page_size,
        "activity": page_activity,
        "sms_activity": [{
            "event_id": event.entity_id,
            "user_id": event.actor_user_id,
            "type": event.action.removeprefix("medhunt_"),
            "candidate_id": (event.meta or {}).get("candidate_id"),
            "candidate_name": (event.meta or {}).get("candidate_name"),
            "conversation_id": (event.meta or {}).get("conversation_id"),
            "used_at": event.created_at,
        } for event in sms_events[:100]],
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
    medhunt_sms_sent = count(select(func.count()).select_from(AuditLog).where(
        AuditLog.actor_user_id == uid,
        AuditLog.action == "medhunt_sms_sent",
        AuditLog.created_at >= since,
    ))
    medhunt_sms_replies = count(select(func.count()).select_from(AuditLog).where(
        AuditLog.actor_user_id == uid,
        AuditLog.action == "medhunt_sms_received",
        AuditLog.created_at >= since,
    ))
    medhunt_sms_assignments = count(select(func.count()).select_from(AuditLog).where(
        AuditLog.actor_user_id == uid,
        AuditLog.action == "medhunt_conversation_assigned",
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
            "sms_sent_recent": medhunt_sms_sent,
            "sms_replies_recent": medhunt_sms_replies,
            "sms_assignments_recent": medhunt_sms_assignments,
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
