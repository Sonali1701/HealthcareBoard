"""AI candidate-matching endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from ..deps import CurrentUser, DbSession
from ..models import MatchResult, MatchRun
from ..schemas.matching import (
    MatchRequest,
    MatchResponse,
    ShortlistUpdate,
)
from ..services.matching import run_matching

router = APIRouter(prefix="/api/matching", tags=["matching"])


@router.post("/run", response_model=MatchResponse)
def run(req: MatchRequest, user: CurrentUser, db: DbSession):
    candidates, summary = run_matching(db, req)

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


@router.get("/runs/{run_id}", response_model=MatchResponse)
def get_run(run_id: str, user: CurrentUser, db: DbSession):
    from ..models import Profile
    from ..schemas.matching import CandidateMatch, MatchSummary, ScoreBreakdown

    run_row = db.get(MatchRun, run_id)
    if not run_row:
        raise HTTPException(status_code=404, detail="Match run not found")
    results = db.scalars(
        select(MatchResult).where(MatchResult.run_id == run_id)
        .order_by(MatchResult.rank.asc())
    ).all()

    candidates = []
    for r in results:
        p = db.get(Profile, r.profile_id)
        candidates.append(CandidateMatch(
            rank=r.rank,
            profile_id=r.profile_id,
            name=f"{p.first_name} {p.last_name}" if p else "Unknown",
            initials=((p.first_name[:1] + p.last_name[:1]).upper() if p else "?"),
            title=p.headline if p else None,
            specialty=p.specialty if p else None,
            location=", ".join(x for x in [p.city, p.state_code] if x) if p else None,
            city=p.city if p else None,
            state_code=p.state_code if p else None,
            years_experience=p.years_experience if p else 0,
            pay_min=float(p.pay_min_hourly) if p and p.pay_min_hourly is not None else None,
            pay_max=float(p.pay_min_hourly) + 15 if p and p.pay_min_hourly is not None else None,
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
    result = db.scalar(
        select(MatchResult).where(MatchResult.run_id == run_id,
                                  MatchResult.profile_id == profile_id)
    )
    if not result:
        raise HTTPException(status_code=404, detail="Candidate not in this run")
    result.shortlisted = body.shortlisted
    db.commit()
    return {"profile_id": profile_id, "shortlisted": result.shortlisted}
