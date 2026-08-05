"""Profile, license, certification, work-history, skill schemas."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field

from ..models.enums import LicenseStatus, ProfileSource
from .common import ORMModel


# --- Licenses -------------------------------------------------------------

class LicenseBase(BaseModel):
    license_type: str
    license_number: str
    state_code: str = Field(min_length=2, max_length=2)
    status: LicenseStatus = LicenseStatus.active
    issued_date: Optional[date] = None
    expiry_date: Optional[date] = None
    verification_source: Optional[str] = None
    is_compact: bool = False


class LicenseCreate(LicenseBase):
    pass


class LicenseOut(ORMModel, LicenseBase):
    license_id: str
    profile_id: str
    verified_at: Optional[datetime] = None


# --- Certifications -------------------------------------------------------

class CertificationBase(BaseModel):
    cert_name: str
    issuing_body: Optional[str] = None
    issue_date: Optional[date] = None
    expiry_date: Optional[date] = None
    cert_number: Optional[str] = None


class CertificationCreate(CertificationBase):
    pass


class CertificationOut(ORMModel, CertificationBase):
    cert_id: str
    profile_id: str


# --- Work history ---------------------------------------------------------

class WorkHistoryBase(BaseModel):
    employer_name: str
    job_title: str
    specialty: Optional[str] = None
    employment_type: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    city: Optional[str] = None
    state_code: Optional[str] = None
    description: Optional[str] = None
    bed_count: Optional[int] = None
    nurse_ratio: Optional[str] = None


class WorkHistoryCreate(WorkHistoryBase):
    pass


class WorkHistoryOut(ORMModel, WorkHistoryBase):
    work_id: str
    profile_id: str


# --- Skills ---------------------------------------------------------------

class SkillBase(BaseModel):
    name: str
    years: Optional[int] = None


class SkillCreate(SkillBase):
    pass


class SkillOut(ORMModel, SkillBase):
    skill_id: str
    profile_id: str


# --- Profiles -------------------------------------------------------------

class ProfileBase(BaseModel):
    first_name: str
    last_name: str
    headline: Optional[str] = None
    bio: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    specialty: Optional[str] = None
    profession_type: Optional[str] = None
    provider_category: Optional[str] = None
    american_board: Optional[str] = None
    years_experience: int = 0
    city: Optional[str] = None
    state_code: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    open_to_work: bool = True
    job_type_prefs: list[str] = Field(default_factory=list)
    pay_min_hourly: Optional[float] = None
    available_date: Optional[date] = None
    npi_number: Optional[str] = None
    profile_photo_url: Optional[str] = None
    resume_url: Optional[str] = None


class ProfileCreate(ProfileBase):
    pass


class ProfileUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    headline: Optional[str] = None
    bio: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    specialty: Optional[str] = None
    profession_type: Optional[str] = None
    provider_category: Optional[str] = None
    american_board: Optional[str] = None
    years_experience: Optional[int] = None
    city: Optional[str] = None
    state_code: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    open_to_work: Optional[bool] = None
    job_type_prefs: Optional[list[str]] = None
    pay_min_hourly: Optional[float] = None
    available_date: Optional[date] = None
    npi_number: Optional[str] = None
    profile_photo_url: Optional[str] = None
    resume_url: Optional[str] = None


class ProfileOut(ORMModel, ProfileBase):
    profile_id: str
    user_id: Optional[str] = None
    contact_updated_by_user_id: Optional[str] = None
    contact_updated_by_email: Optional[str] = None
    contact_updated_at: Optional[datetime] = None
    completion_score: int
    source: ProfileSource
    created_at: datetime
    updated_at: datetime


class ProfileCardOut(BaseModel):
    """A provider row in the recruiter directory, with identity withheld.

    Name and contact are omitted from the payload entirely until the recruiter
    deliberately releases the profile (POST /profiles/{id}/contact-release),
    which is audit-logged. Masking only in the browser would leave the real
    values sitting in the network response, so they are never serialised here.
    """

    profile_id: str
    # Always present, safe to show: "T. H." and "TH".
    masked_name: str
    initials: str
    is_released: bool = False
    # Populated only once released.
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    contact_updated_by_email: Optional[str] = None
    # Enough to drive the UI without revealing the values themselves.
    has_email: bool = False
    has_phone: bool = False
    # Non-identifying professional attributes.
    headline: Optional[str] = None
    specialty: Optional[str] = None
    profession_type: Optional[str] = None
    provider_category: Optional[str] = None
    american_board: Optional[str] = None
    years_experience: int = 0
    city: Optional[str] = None
    state_code: Optional[str] = None
    completion_score: int = 0


class ProfileDetail(ProfileOut):
    licenses: list[LicenseOut] = Field(default_factory=list)
    certifications: list[CertificationOut] = Field(default_factory=list)
    work_history: list[WorkHistoryOut] = Field(default_factory=list)
    skills: list[SkillOut] = Field(default_factory=list)
