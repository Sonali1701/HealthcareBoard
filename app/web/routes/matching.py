"""Recruiter AI matching page."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ...database import get_db
from ...models import MatchResult, MatchRun
from ...schemas.matching import MatchRequest, MatchWeights
from ...services.matching import run_matching
from ..core import RedirectException, render, require_user

router = APIRouter(tags=["web-matching"])
DbDep = Annotated[Session, Depends(get_db)]


def _require_recruiter(user):
    if user.role.value not in ("recruiter", "employer", "admin"):
        raise RedirectException("/dashboard")


@router.get("/matching")
def matching_form(request: Request, user=Depends(require_user)):
    _require_recruiter(user)
    return render(request, "recruiter/matching.html",
                  {"active": "matching", "results": None, "spec": {}, "user": user})


@router.post("/matching")
def matching_run(request: Request, db: DbDep, user=Depends(require_user),
                 specialty: Annotated[str, Form()] = "",
                 profession_type: Annotated[str, Form()] = "",
                 required_skills: Annotated[str, Form()] = "",
                 state: Annotated[str, Form()] = "",
                 w_skills: Annotated[float, Form()] = 35, w_exp: Annotated[float, Form()] = 25,
                 w_loc: Annotated[float, Form()] = 20, w_pay: Annotated[float, Form()] = 20):
    _require_recruiter(user)
    req = MatchRequest(
        specialty=specialty.strip() or None,
        profession_type=profession_type.strip() or None,
        required_skills=[s.strip() for s in required_skills.split(",") if s.strip()],
        weights=MatchWeights(skills=w_skills, experience=w_exp, location=w_loc, pay=w_pay),
        top_n=50,
    )
    candidates, summary = run_matching(db, req)
    if not candidates and specialty.strip():
        # Fall back to all open candidates if the specialty filter is too narrow.
        req.specialty = None
        candidates, summary = run_matching(db, req)

    # Persist the run (so it appears in history / API too).
    run = MatchRun(requested_by_user_id=user.user_id,
                   job_spec=req.model_dump(mode="json", exclude={"weights", "filters"}),
                   weights=req.weights.model_dump(), candidate_count=summary.total,
                   avg_score=summary.avg_score)
    db.add(run)
    db.flush()
    for c in candidates:
        db.add(MatchResult(run_id=run.run_id, profile_id=c.profile_id, rank=c.rank,
                           score_total=c.score_total, score_skills=c.score_breakdown.skills,
                           score_experience=c.score_breakdown.experience,
                           score_location=c.score_breakdown.location,
                           score_pay=c.score_breakdown.pay, match_reason=c.match_reason))
    db.commit()

    return render(request, "recruiter/matching.html",
                  {"active": "matching", "results": candidates, "summary": summary,
                   "spec": {"specialty": specialty, "profession_type": profession_type,
                            "required_skills": required_skills, "state": state}, "user": user})
