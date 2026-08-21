"""AI candidate-matching endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ..deps import CurrentUser, DbSession
from ..models import AuditLog, MatchResult, MatchRun
from ..schemas.matching import (
    MatchRequest,
    MatchResponse,
    ShortlistUpdate,
)
from ..services.matching import run_matching

router = APIRouter(prefix="/api/matching", tags=["matching"])


def _released_for(db: DbSession, user: CurrentUser) -> set[str]:
    """Every profile this recruiter has already released. Matching scores a pool
    of thousands, so this is fetched once rather than per candidate."""
    from .profiles import RELEASE_ACTION
    rows = db.scalars(
        select(AuditLog.entity_id).where(AuditLog.actor_user_id == user.user_id,
                                         AuditLog.action == RELEASE_ACTION)
    ).all()
    return {r for r in rows if r}


def _require_recruiter(user: CurrentUser) -> None:
    if user.role.value not in {"recruiter", "admin"}:
        raise HTTPException(status_code=403,
                            detail="Candidate matching is available to recruiters only")


def _own_run_or_404(db: DbSession, run_id: str, user: CurrentUser) -> MatchRun:
    run_row = db.get(MatchRun, run_id)
    if not run_row:
        raise HTTPException(status_code=404, detail="Match run not found")
    if run_row.requested_by_user_id != user.user_id and user.role.value != "admin":
        raise HTTPException(status_code=403, detail="This match run belongs to another recruiter")
    return run_row


@router.post("/run", response_model=MatchResponse)
def run(req: MatchRequest, user: CurrentUser, db: DbSession):
    _require_recruiter(user)
    candidates, summary = run_matching(db, req, released=_released_for(db, user))

    run_row = MatchRun(
        job_id=req.job_id,
        requested_by_user_id=user.user_id,
        job_spec=req.model_dump(mode="json", exclude={"weights", "filters"}),
        weights=req.weights.model_dump(),
        candidate_count=summary.total,
        avg_score=summary.avg_score,
    )
    db.add(run_row)
    db.flush()
    for c in candidates:
        db.add(MatchResult(
            run_id=run_row.run_id,
            profile_id=c.profile_id,
            rank=c.rank,
            score_total=c.score_total,
            score_skills=c.score_breakdown.skills,
            score_experience=c.score_breakdown.experience,
            score_location=c.score_breakdown.location,
            score_pay=c.score_breakdown.pay,
            match_reason=c.match_reason,
        ))
    db.commit()

    return MatchResponse(run_id=run_row.run_id, summary=summary, candidates=candidates)


@router.get("/runs")
def list_runs(user: CurrentUser, db: DbSession, limit: int = 20):
    """This recruiter's recent sourcing runs, newest first."""
    _require_recruiter(user)
    from ..models import JobPosting
    runs = db.scalars(
        select(MatchRun).where(MatchRun.requested_by_user_id == user.user_id)
        .order_by(MatchRun.created_at.desc()).limit(limit)
    ).all()
    titles = {
        j.job_id: j.title for j in db.scalars(
            select(JobPosting).where(
                JobPosting.job_id.in_([r.job_id for r in runs if r.job_id]))
        )
    } if runs else {}
    return [{
        "run_id": r.run_id,
        "job_id": r.job_id,
        "job_title": titles.get(r.job_id) or (r.job_spec or {}).get("job_title"),
        "candidate_count": r.candidate_count,
        "avg_score": float(r.avg_score) if r.avg_score is not None else None,
        "created_at": r.created_at,
    } for r in runs]


@router.get("/runs/{run_id}", response_model=MatchResponse)
def get_run(run_id: str, user: CurrentUser, db: DbSession):
    from ..models import Profile
    from ..schemas.matching import CandidateMatch, MatchSummary, ScoreBreakdown
    from .profiles import _masked_name

    _require_recruiter(user)
    run_row = _own_run_or_404(db, run_id, user)
    results = db.scalars(
        select(MatchResult).where(MatchResult.run_id == run_id)
        .order_by(MatchResult.rank.asc())
    ).all()
    # One query for the whole page instead of a db.get() per candidate.
    profiles = {
        p.profile_id: p for p in db.scalars(
            select(Profile).where(Profile.profile_id.in_([r.profile_id for r in results]))
        )
    } if results else {}
    released = _released_for(db, user)

    candidates = []
    for r in results:
        p = profiles.get(r.profile_id)
        is_open = r.profile_id in released
        candidates.append(CandidateMatch(
            rank=r.rank,
            profile_id=r.profile_id,
            name=(f"{p.first_name or ''} {p.last_name or ''}".strip() or "Unnamed"
                  if is_open else _masked_name(p)) if p else "Unknown",
            is_released=is_open,
            initials=((p.first_name[:1] + p.last_name[:1]).upper() if p else "?"),
            title=p.headline if p else None,
            specialty=p.specialty if p else None,
            location=", ".join(x for x in [p.city, p.state_code] if x) if p else None,
            city=p.city if p else None,
            state_code=p.state_code if p else None,
            years_experience=p.years_experience if p else 0,
            pay_min=float(p.pay_min_hourly) if p and p.pay_min_hourly is not None else None,
            # A candidate states a desired floor, not a ceiling — don't invent
            # one. (This used to report pay_min + $15 for everyone.)
            pay_max=None,
            skills=[s.name for s in p.skills] if p else [],
            certifications=[c.cert_name for c in p.certifications] if p else [],
            completion_score=p.completion_score if p else 0,
            score_total=float(r.score_total),
            score_breakdown=ScoreBreakdown(
                skills=float(r.score_skills or 0),
                experience=float(r.score_experience or 0),
                location=float(r.score_location or 0),
                pay=float(r.score_pay or 0),
            ),
            travel_experienced="travel" in (p.job_type_prefs or []) if p else False,
            immediately_available=bool(p.open_to_work) if p else False,
            match_reason=r.match_reason or "",
        ))

    n = len(candidates)
    summary = MatchSummary(
        total=n,
        excellent_90plus=sum(1 for c in candidates if c.score_total >= 90),
        great_80_89=sum(1 for c in candidates if 80 <= c.score_total < 90),
        good_70_79=sum(1 for c in candidates if 70 <= c.score_total < 80),
        avg_score=float(run_row.avg_score or 0),
        immediately_available=sum(1 for c in candidates if c.immediately_available),
    )
    return MatchResponse(run_id=run_id, summary=summary, candidates=candidates)


@router.post("/runs/{run_id}/candidates/{profile_id}/shortlist")
def shortlist(run_id: str, profile_id: str, body: ShortlistUpdate,
              user: CurrentUser, db: DbSession):
    _require_recruiter(user)
    _own_run_or_404(db, run_id, user)
    result = db.scalar(
        select(MatchResult).where(MatchResult.run_id == run_id,
                                  MatchResult.profile_id == profile_id)
    )
    if not result:
        raise HTTPException(status_code=404, detail="Candidate not in this run")
    result.shortlisted = body.shortlisted
    db.commit()
    return {"profile_id": profile_id, "shortlisted": result.shortlisted}
