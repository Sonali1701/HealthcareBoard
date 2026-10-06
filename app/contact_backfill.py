"""Schedule contactless Neon profiles into Medhunt's shared priority queue.

Render should run ``python -m app.contact_backfill`` every ten minutes. The
command exits immediately outside the Pacific overnight/weekend window.
"""
from __future__ import annotations

import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import and_, func, or_, select, text

from .config import settings
from .database import SessionLocal, utcnow
from .models import Profile, ProfileContactBackfill

logger = logging.getLogger("healthboard.contact_backfill")
PACIFIC = ZoneInfo("America/Los_Angeles")


def window_open(now: datetime | None = None) -> bool:
    local = (now or datetime.now(timezone.utc)).astimezone(PACIFIC)
    return local.weekday() >= 5 or local.hour >= 18 or local.hour < 6


def _missing(column):
    return or_(column.is_(None), func.length(func.trim(column)) == 0)


def run_once() -> dict:
    if not window_open():
        return {"status": "outside_window", "queued": 0}
    if not settings.medhunt_api_base_url or not settings.medhunt_service_token:
        raise RuntimeError("MEDHUNT_API_BASE_URL and MEDHUNT_SERVICE_TOKEN are required")

    now = utcnow()
    stale = now - timedelta(hours=1)
    batch_size = max(1, min(100, int(settings.contact_backfill_batch_size)))
    run_id = f"halo-{uuid.uuid4().hex[:24]}"
    db = SessionLocal()
    try:
        if not settings.database_url.startswith("sqlite"):
            locked = db.execute(text(
                "SELECT pg_try_advisory_xact_lock(hashtext('halo-contact-backfill-scheduler'))"
            )).scalar()
            if not locked:
                return {"status": "already_running", "queued": 0}
        rows = db.execute(
            select(Profile, ProfileContactBackfill)
            .outerjoin(ProfileContactBackfill, ProfileContactBackfill.profile_id == Profile.profile_id)
            .where(
                Profile.is_listable.is_(True),
                func.length(func.trim(Profile.first_name)) > 0,
                func.length(func.trim(Profile.last_name)) > 0,
                func.length(func.trim(Profile.city)) > 0,
                func.length(func.trim(Profile.state_code)) > 0,
                _missing(Profile.email), _missing(Profile.phone),
                or_(
                    ProfileContactBackfill.profile_id.is_(None),
                    and_(ProfileContactBackfill.status == "not_found",
                         ProfileContactBackfill.next_eligible_at <= now),
                    and_(ProfileContactBackfill.status == "failed",
                         ProfileContactBackfill.next_eligible_at <= now),
                    and_(ProfileContactBackfill.status.in_(("queued", "processing")),
                         ProfileContactBackfill.updated_at < stale),
                ),
            )
            .order_by(Profile.updated_at.desc(), Profile.profile_id)
            .limit(batch_size)
        ).all()
        if not rows:
            db.commit()
            return {"status": "idle", "queued": 0}

        profiles = []
        for profile, state in rows:
            if state is None:
                state = ProfileContactBackfill(profile_id=profile.profile_id)
                db.add(state)
            state.status = "queued"
            state.run_id = run_id
            state.last_attempt_at = now
            state.next_eligible_at = None
            state.last_error = None
            profiles.append({
                "profile_id": profile.profile_id,
                "name": f"{profile.first_name} {profile.last_name}".strip(),
                "location": f"{profile.city}, {profile.state_code}",
            })
        db.commit()

        response = httpx.post(
            f"{settings.medhunt_api_base_url.rstrip('/')}/internal/halo/contact-backfill",
            headers={"X-Medhunt-Service-Token": settings.medhunt_service_token},
            json={"run_id": run_id, "profiles": profiles},
            timeout=30,
        )
        response.raise_for_status()
        accepted = response.json().get("items") or []
        by_profile = {str(item.get("profile_id")): item for item in accepted}
        # A fast worker may already have called back while the enqueue request
        # was returning. Reload state so this scheduler never changes an
        # enriched/processing row back to queued.
        db.expire_all()
        for payload in profiles:
            state = db.get(ProfileContactBackfill, payload["profile_id"])
            item = by_profile.get(payload["profile_id"], {})
            if state:
                state.medhunt_candidate_id = item.get("candidate_id")
                if not item and state.status == "queued":
                    state.status = "failed"
                    state.last_error = "Medhunt did not accept this profile"
                    state.next_eligible_at = now + timedelta(minutes=15)
        db.commit()
        return {"status": "queued", "queued": len(accepted), "run_id": run_id}
    except Exception as exc:
        db.rollback()
        for payload in locals().get("profiles", []):
            state = db.get(ProfileContactBackfill, payload["profile_id"])
            if state and state.run_id == run_id:
                state.status = "failed"
                state.last_error = str(exc)[:1000]
                state.next_eligible_at = now + timedelta(minutes=15)
        db.commit()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        logger.info("Halo contact backfill: %s", run_once())
    except Exception:
        logger.exception("Halo contact backfill failed")
        sys.exit(1)
