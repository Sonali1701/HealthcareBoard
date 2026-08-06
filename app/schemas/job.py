"""Employer, job posting, application, saved-job schemas."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field

from ..models.enums import (
    ApplicationStatus,
    JobStatus,
    JobType,
    SubscriptionTier,
)
from .common import ORMModel


# --- Employers ------------------------------------------------------------

class EmployerBase(BaseModel):
    org_name: str
    org_type: Optional[str] = None
    logo_url: Optional[str] = None
    website_url: Optional[str] = None
    description: Optional[str] = None
    city: Optional[str] = None
    state_code: Optional[str] = None
    bed_count: Optional[int] = None


class EmployerCreate(EmployerBase):
    pass


class EmployerUpdate(BaseModel):
    org_name: Optional[str] = None
    org_type: Optional[str] = None
    logo_url: Optional[str] = None
    website_url: Optional[str] = None
    description: Optional[str] = None
    city: Optional[str] = None
    state_code: Optional[str] = None
    bed_count: Optional[int] = None


class EmployerOut(ORMModel, EmployerBase):
    employer_id: str
    owner_user_id: str
    is_verified: bool
    rating_avg: float
    subscription_tier: SubscriptionTier
    job_credits_balance: int
    created_at: datetime


# --- Job postings ---------------------------------------------------------

class JobBase(BaseModel):
    title: str
    specialty: Optional[str] = None
    profession_type: Optional[str] = None
    job_type: JobType = JobType.travel
    shift_type: Optional[str] = None
    pay_rate_min: Optional[float] = None
    pay_rate_max: Optional[float] = None
    pay_unit: str = "hourly"
    housing_stipend: Optional[float] = None
    signing_bonus: Optional[float] = None
    city: Optional[str] = None
    state_code: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    description: Optional[str] = None
    requirements: dict = Field(default_factory=dict)
    benefits: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    required_certifications: list[str] = Field(default_factory=list)
    years_exp_min: Optional[int] = None
    is_urgent: bool = False
    is_featured: bool = False
    start_date: Optional[date] = None
    expires_at: Optional[datetime] = None


class JobCreate(JobBase):
    pass


class JobUpdate(BaseModel):
    title: Optional[str] = None
    specialty: Optional[str] = None
    profession_type: Optional[str] = None
    job_type: Optional[JobType] = None
    shift_type: Optional[str] = None
    pay_rate_min: Optional[float] = None
    pay_rate_max: Optional[float] = None
    pay_unit: Optional[str] = None
    housing_stipend: Optional[float] = None
    signing_bonus: Optional[float] = None
    city: Optional[str] = None
    state_code: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    description: Optional[str] = None
    requirements: Optional[dict] = None
    benefits: Optional[list[str]] = None
    required_skills: Optional[list[str]] = None
    required_certifications: Optional[list[str]] = None
    years_exp_min: Optional[int] = None
    is_urgent: Optional[bool] = None
    is_featured: Optional[bool] = None
    status: Optional[JobStatus] = None
    start_date: Optional[date] = None
    expires_at: Optional[datetime] = None


class JobOut(ORMModel, JobBase):
    job_id: str
    employer_id: str
    status: JobStatus
    view_count: int
    application_count: int
    facility: Optional[str] = None
    agency: Optional[str] = None
    req_code: Optional[str] = None
    # Seats open for this same role/facility, when the list is grouped.
    openings: int = 1
    # How well the role matches the signed-in professional (recommendations only).
    fit_score: int = 0
    created_at: datetime
    updated_at: datetime


# --- Applications ---------------------------------------------------------

class ApplicationCreate(BaseModel):
    # job_id comes from the URL path; optional here for convenience.
    job_id: Optional[str] = None
    profile_id: Optional[str] = None  # defaults to current user's profile
    cover_letter: Optional[str] = None
    resume_snapshot_url: Optional[str] = None
    source: str = "platform"


class ApplicationStageUpdate(BaseModel):
    status: ApplicationStatus
    note: Optional[str] = None
    recruiter_rating: Optional[int] = Field(default=None, ge=1, le=5)


class ApplicationEventOut(ORMModel):
    event_id: str
    from_status: Optional[str] = None
    to_status: str
    note: Optional[str] = None
    created_at: datetime


class ApplicationOut(ORMModel):
    application_id: str
    job_id: str
    profile_id: str
    status: ApplicationStatus
    cover_letter: Optional[str] = None
    match_score: Optional[float] = None
    recruiter_notes: Optional[str] = None
    recruiter_rating: Optional[int] = None
    source: str
    applied_at: datetime
    status_updated_at: datetime


# --- Saved jobs -----------------------------------------------------------

class SavedJobOut(ORMModel):
    save_id: str
    profile_id: str
    job_id: str
    saved_at: datetime
