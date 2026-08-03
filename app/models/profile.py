"""Domain 2 — Healthcare Profiles: profiles, licenses, work_history, certifications, skills."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Enum,
    Float,
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base, TZDateTime, created_col, updated_col, uuid_fk, uuid_pk
from .enums import LicenseStatus, ProfileSource


class Profile(Base):
    __tablename__ = "profiles"

    profile_id: Mapped[str] = uuid_pk()
    # Nullable: imported profiles may not have a linked user account.
    user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True, index=True
    )
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    headline: Mapped[Optional[str]] = mapped_column(String(255))
    bio: Mapped[Optional[str]] = mapped_column(Text)
    phone: Mapped[Optional[str]] = mapped_column(String(30), index=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    specialty: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    profession_type: Mapped[Optional[str]] = mapped_column(String(50), index=True)
    # Physicians | Nursing | Allied | APP | Others (derived from résumé evidence).
    provider_category: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    # Primary certifying board, e.g. "American Board of Allergy and Immunology".
    american_board: Mapped[Optional[str]] = mapped_column(String(150), index=True)
    # False = parser produced junk (placeholder name); hidden from the directory.
    is_listable: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    contact_updated_by_user_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    contact_updated_by_email: Mapped[Optional[str]] = mapped_column(String(255))
    contact_updated_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    years_experience: Mapped[int] = mapped_column(SmallInteger, default=0, index=True)
    city: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    state_code: Mapped[Optional[str]] = mapped_column(String(2), index=True)
    zip_code: Mapped[Optional[str]] = mapped_column(String(10), index=True)
    # Geocoded from zip (precise) or city+state (centroid) for distance search.
    lat: Mapped[Optional[float]] = mapped_column(Float)
    lng: Mapped[Optional[float]] = mapped_column(Float)
    open_to_work: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    job_type_prefs: Mapped[list] = mapped_column(JSON, default=list)  # travel|staff|per_diem|contract
    pay_min_hourly: Mapped[Optional[float]] = mapped_column(Numeric(8, 2), index=True)
    available_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    npi_number: Mapped[Optional[str]] = mapped_column(String(10), unique=True)
    profile_photo_url: Mapped[Optional[str]] = mapped_column(Text)
    resume_url: Mapped[Optional[str]] = mapped_column(Text)
    completion_score: Mapped[int] = mapped_column(SmallInteger, default=0, index=True)
    source: Mapped[ProfileSource] = mapped_column(
        Enum(ProfileSource), default=ProfileSource.signup, index=True
    )
    # Denormalised lowercase searchable text (portable substitute for TSVECTOR).
    search_text: Mapped[Optional[str]] = mapped_column(Text, index=True)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()

    user: Mapped[Optional["User"]] = relationship(back_populates="profile")  # noqa: F821
    licenses: Mapped[list["License"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    work_history: Mapped[list["WorkHistory"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    certifications: Mapped[list["Certification"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    skills: Mapped[list["ProfileSkill"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )

    def rebuild_search_text(self) -> None:
        from ..importers.parsing import LICENSE_FULL_NAMES
        parts = [
            self.first_name, self.last_name, self.headline, self.bio,
            self.specialty, self.profession_type, self.city, self.state_code,
            self.american_board, self.provider_category,
            # Full license name so "registered nurse" matches an RN profile.
            LICENSE_FULL_NAMES.get((self.profession_type or "").upper().strip(".")),
        ]
        self.search_text = " ".join(p for p in parts if p).lower()


class License(Base):
    __tablename__ = "licenses"

    license_id: Mapped[str] = uuid_pk()
    profile_id: Mapped[str] = uuid_fk("profiles.profile_id")
    license_type: Mapped[str] = mapped_column(String(50), index=True)
    license_number: Mapped[str] = mapped_column(String(100), index=True)
    state_code: Mapped[str] = mapped_column(String(2), index=True)
    status: Mapped[LicenseStatus] = mapped_column(
        Enum(LicenseStatus), default=LicenseStatus.active, index=True
    )
    issued_date: Mapped[Optional[date]] = mapped_column(Date)
    expiry_date: Mapped[Optional[date]] = mapped_column(Date, index=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    verification_source: Mapped[Optional[str]] = mapped_column(String(100))
    is_compact: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = created_col()

    profile: Mapped[Profile] = relationship(back_populates="licenses")


class WorkHistory(Base):
    __tablename__ = "work_history"

    work_id: Mapped[str] = uuid_pk()
    profile_id: Mapped[str] = uuid_fk("profiles.profile_id")
    employer_name: Mapped[str] = mapped_column(String(200))
    job_title: Mapped[str] = mapped_column(String(200))
    specialty: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    employment_type: Mapped[Optional[str]] = mapped_column(String(50))  # staff|travel|per_diem|agency
    start_date: Mapped[Optional[date]] = mapped_column(Date)
    end_date: Mapped[Optional[date]] = mapped_column(Date)  # null = current
    city: Mapped[Optional[str]] = mapped_column(String(120))
    state_code: Mapped[Optional[str]] = mapped_column(String(2))
    description: Mapped[Optional[str]] = mapped_column(Text)
    bed_count: Mapped[Optional[int]] = mapped_column(SmallInteger)
    nurse_ratio: Mapped[Optional[str]] = mapped_column(String(20))
    created_at: Mapped[datetime] = created_col()

    profile: Mapped[Profile] = relationship(back_populates="work_history")


class Certification(Base):
    __tablename__ = "certifications"

    cert_id: Mapped[str] = uuid_pk()
    profile_id: Mapped[str] = uuid_fk("profiles.profile_id")
    cert_name: Mapped[str] = mapped_column(String(100), index=True)  # ACLS|BLS|CCRN|TNCC...
    issuing_body: Mapped[Optional[str]] = mapped_column(String(100))
    issue_date: Mapped[Optional[date]] = mapped_column(Date)
    expiry_date: Mapped[Optional[date]] = mapped_column(Date)
    cert_number: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[datetime] = created_col()

    profile: Mapped[Profile] = relationship(back_populates="certifications")


class ProfileSkill(Base):
    __tablename__ = "profile_skills"

    skill_id: Mapped[str] = uuid_pk()
    profile_id: Mapped[str] = uuid_fk("profiles.profile_id")
    name: Mapped[str] = mapped_column(String(100), index=True)
    years: Mapped[Optional[int]] = mapped_column(SmallInteger)

    profile: Mapped[Profile] = relationship(back_populates="skills")
