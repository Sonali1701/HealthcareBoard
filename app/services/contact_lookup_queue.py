"""Postgres-backed worker for slow public-record contact searches.

Each web worker runs one consumer. Jobs are claimed with SKIP LOCKED, so a
request storm is stored in Neon and drained at a controlled rate instead of
starting one browser search per HTTP request. The job survives refreshes and
worker restarts.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select, update

from ..config import settings
from ..database import SessionLocal, utcnow
from ..models import AuditLog, ContactLookupJob, Profile, User
from . import quick_sourcer

logger = logging.getLogger("healthboard.contact_lookup_queue")
LOOKUP_ACTION = "provider_contact_lookup"
RELEASE_ACTION = "provider_contact_released"
POLL_SECONDS = 0.75
STALE_AFTER = timedelta(minutes=5)


def _claim_one() -> str | None:
    """Claim one queued job atomically; called in a thread, never on ASGI loop."""
    now = utcnow()
    with SessionLocal.begin() as db:
        # A process can die during the upstream call. Its lease expires well
        # after the configured 150-second upstream timeout and the job is retried.
        db.execute(update(ContactLookupJob).where(
            ContactLookupJob.status == "processing",
            ContactLookupJob.updated_at < now - STALE_AFTER,
        ).values(status="queued", detail="Worker restarted; queued for retry.", updated_at=now))
        job = db.scalar(
            select(ContactLookupJob)
            .where(ContactLookupJob.status == "queued")
            .order_by(ContactLookupJob.created_at, ContactLookupJob.job_id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not job:
            return None
        job.status = "processing"
        job.attempts += 1
        job.detail = None
        job.updated_at = now
        return job.job_id


def _job_context(job_id: str) -> tuple[str, str, str, str | None] | None:
    with SessionLocal() as db:
        job = db.get(ContactLookupJob, job_id)
        if not job or job.status != "processing":
            return None
        profile = db.get(Profile, job.profile_id)
        user = db.get(User, job.requester_user_id)
        if not profile or not user:
            return None
        name = " ".join((profile.first_name or "").split() + (profile.last_name or "").split())
        location = ", ".join(v.strip() for v in (profile.city, profile.state_code) if v and v.strip()) or None
        prior = db.scalar(
            select(AuditLog)
            .where(AuditLog.action == LOOKUP_ACTION, AuditLog.entity_id == profile.profile_id)
            .order_by(AuditLog.created_at.desc())
        )
        candidate_id = (prior.meta or {}).get("candidate_id") if prior else None
        if not isinstance(candidate_id, int):
            candidate_id = None
        return name, location or "", user.user_id, str(candidate_id) if candidate_id is not None else None


def _store_miss(job_id: str, name: str, location: str, match) -> None:
    with SessionLocal.begin() as db:
        job = db.get(ContactLookupJob, job_id)
        if not job:
            return
        job.status = "not_found"
        job.found = False
        job.source = match.source
        job.detail = ("No contact found this time. The source may be busy; try again later.")
        job.updated_at = utcnow()
        db.add(AuditLog(
            actor_user_id=job.requester_user_id, action=LOOKUP_ACTION,
            entity_type="profile", entity_id=job.profile_id,
            meta={"found": False, "searched": name, "location": location or None,
                  "candidate_id": match.candidate_id, "source": match.source},
        ))


def _store_match(job_id: str, name: str, location: str, match) -> None:
    from . import credits as credit_service

    with SessionLocal.begin() as db:
        job = db.get(ContactLookupJob, job_id)
        if not job:
            return
        profile = db.get(Profile, job.profile_id)
        user = db.get(User, job.requester_user_id)
        if not profile or not user:
            job.status = "failed"
            job.detail = "The profile or recruiter account is no longer available."
            job.updated_at = utcnow()
            return

        charged = {"cost": 0, "balance": None}
        if settings.credits_enabled:
            from ..models import COST_REVEAL_CONTACT
            charged = credit_service.charge(
                db, user.user_id, COST_REVEAL_CONTACT,
                entity_type="profile", entity_id=profile.profile_id,
                idempotency_key=f"reveal:{user.user_id}:{profile.profile_id}",
                note="Contact lookup",
            )

        filled: list[str] = []
        if match.email and not profile.email:
            profile.email = match.email
            filled.append("email")
        if match.phone and not profile.phone:
            profile.phone = match.phone
            filled.append("phone")
        if filled:
            profile.contact_updated_by_user_id = user.user_id
            profile.contact_updated_by_email = user.email
            profile.contact_updated_at = utcnow()
            profile.rebuild_search_text()
            from ..routers.profiles import _compute_completion
            profile.completion_score = _compute_completion(profile)

        prior_release = db.scalar(select(AuditLog.log_id).where(
            AuditLog.actor_user_id == user.user_id,
            AuditLog.action == RELEASE_ACTION,
            AuditLog.entity_id == profile.profile_id,
        ))
        db.add(AuditLog(
            actor_user_id=user.user_id, action=LOOKUP_ACTION,
            entity_type="profile", entity_id=profile.profile_id,
            meta={"found": True, "searched": name, "location": location or None,
                  "source": match.source, "candidate_id": match.candidate_id,
                  "filled": filled, "email_found": bool(match.email),
                  "phone_found": bool(match.phone)},
        ))
        if not prior_release:
            db.add(AuditLog(
                actor_user_id=user.user_id, action=RELEASE_ACTION,
                entity_type="profile", entity_id=profile.profile_id,
                meta={"via": "quick_sourcer", "source": match.source,
                      "email_available": bool(profile.email),
                      "phone_available": bool(profile.phone)},
            ))
        job.status = "completed"
        job.found = True
        job.source = match.source
        job.detail = None
        job.credits_charged = int(charged.get("cost", 0) or 0)
        job.credits_remaining = charged.get("balance")
        job.updated_at = utcnow()


def _store_failure(job_id: str, detail: str) -> None:
    with SessionLocal.begin() as db:
        job = db.get(ContactLookupJob, job_id)
        if job:
            job.status = "failed"
            job.found = False
            job.detail = detail[:500]
            job.updated_at = utcnow()


async def _run_one(job_id: str) -> None:
    context = await asyncio.to_thread(_job_context, job_id)
    if not context:
        await asyncio.to_thread(_store_failure, job_id, "Profile or recruiter account was not found.")
        return
    name, location, _user_id, prior_candidate_id = context
    if not name or " " not in name:
        await asyncio.to_thread(_store_failure, job_id,
                                "A full first and last name is needed. Update the profile, then try again.")
        return
    try:
        match = None
        if prior_candidate_id is not None:
            try:
                match = await quick_sourcer.fetch_candidate(int(prior_candidate_id))
            except quick_sourcer.QuickSourcerError:
                match = None
        if match is None or not match.found:
            match = await quick_sourcer.find(name, location or None)
        if not match.found or not (match.email or match.phone):
            await asyncio.to_thread(_store_miss, job_id, name, location, match)
            return
        try:
            await asyncio.to_thread(_store_match, job_id, name, location, match)
        except Exception as exc:
            from .credits import InsufficientCredits
            if isinstance(exc, InsufficientCredits):
                await asyncio.to_thread(
                    _store_failure, job_id,
                    f"Contact found, but this account needs {exc.needed} credit to unlock it and has {exc.balance}.")
                return
            raise
    except quick_sourcer.QuickSourcerError as exc:
        await asyncio.to_thread(_store_failure, job_id, str(exc))
    except Exception:
        logger.exception("contact lookup job failed (job_id=%s)", job_id)
        await asyncio.to_thread(_store_failure, job_id,
                                "The lookup failed unexpectedly. Try again.")


async def worker_loop() -> None:
    """Drain jobs continuously; each app process runs one bounded consumer."""
    while True:
        try:
            job_id = await asyncio.to_thread(_claim_one)
            if job_id:
                await _run_one(job_id)
            else:
                await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("contact lookup queue poll failed")
            await asyncio.sleep(2)
