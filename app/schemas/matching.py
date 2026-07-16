"""AI matching engine request/response schemas."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .common import ORMModel


class MatchWeights(BaseModel):
    skills: float = 35
    experience: float = 25
    location: float = 20
    pay: float = 20


class MatchFilters(BaseModel):
    verified_license_only: bool = False
    travel_experienced_boost: bool = False
    immediately_available_only: bool = False


class MatchLocation(BaseModel):
    city: Optional[str] = None
    state_code: Optional[str] = None
    radius_miles: Optional[float] = None


class MatchRequest(BaseModel):
    # Either reference an existing job, or pass an ad-hoc spec (fields below).
    job_id: Optional[str] = None
    job_title: Optional[str] = None
    specialty: Optional[str] = None
    profession_type: Optional[str] = None
    job_type: Optional[str] = None
    pay_rate_min: Optional[float] = None
    pay_rate_max: Optional[float] = None
    required_skills: list[str] = Field(default_factory=list)
    required_certifications: list[str] = Field(default_factory=list)
    years_exp_min: Optional[int] = None
    years_exp_preferred: Optional[int] = None
    location: MatchLocation = Field(default_factory=MatchLocation)
    weights: MatchWeights = Field(default_factory=MatchWeights)
    filters: MatchFilters = Field(default_factory=MatchFilters)
    top_n: int = Field(default=100, ge=1, le=500)


class ScoreBreakdown(BaseModel):
    skills: float
    experience: float
    location: float
    pay: float


class CandidateMatch(BaseModel):
    rank: int
    profile_id: str
    name: str
    initials: str
    title: Optional[str] = None
    specialty: Optional[str] = None
    location: Optional[str] = None
    city: Optional[str] = None
    state_code: Optional[str] = None
    years_experience: int
    pay_min: Optional[float] = None
    pay_max: Optional[float] = None
    skills: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    completion_score: int = 0
    score_total: float
    score_breakdown: ScoreBreakdown
    travel_experienced: bool
    immediately_available: bool
    match_reason: str


class MatchSummary(BaseModel):
    total: int
    excellent_90plus: int
    great_80_89: int
    good_70_79: int
    avg_score: float
    immediately_available: int


class MatchResponse(BaseModel):
    run_id: str
    summary: MatchSummary
    candidates: list[CandidateMatch]


class ShortlistUpdate(BaseModel):
    shortlisted: bool = True


class MatchRunOut(ORMModel):
    run_id: str
    job_id: Optional[str] = None
    candidate_count: int
    avg_score: Optional[float] = None
    created_at: object
