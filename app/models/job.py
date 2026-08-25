"""Domain 3 — Jobs & Applications: employers, employer_members, job_postings,
applications, application_events, saved_jobs."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Enum,
    Float,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base, TZDateTime, created_col, updated_col, uuid_fk, uuid_pk
from .enums import ApplicationStatus, JobStatus, JobType, SubscriptionTier


class Employer(Base):
    __tablename__ = "employers"

    employer_id: Mapped[str] = uuid_pk()
    owner_user_id: Mapped[str] = uuid_fk("users.user_id")
    org_name: Mapped[str] = mapped_column(String(300), nullable=False)
    org_type: Mapped[Optional[str]] = mapped_column(String(50), index=True)  # health_system|hospital|agency|clinic
    logo_url: Mapped[Optional[str]] = mapped_column(Text)
    website_url: Mapped[Optional[str]] = mapped_column(Text)
    description: Mapped[Optional[str]] = mapped_column(Text)
    city: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    state_code: Mapped[Optional[str]] = mapped_column(String(2), index=True)
    bed_count: Mapped[Optional[int]] = mapped_column(Integer)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    rating_avg: Mapped[float] = mapped_column(Numeric(3, 2), default=0)
    subscription_tier: Mapped[SubscriptionTier] = mapped_column(
        Enum(SubscriptionTier), default=SubscriptionTier.free, index=True
    )
    job_credits_balance: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()

    members: Mapped[list["EmployerMember"]] = relationship(
        back_populates="employer", cascade="all, delete-orphan"
    )
    jobs: Mapped[list["JobPosting"]] = relationship(
        back_populates="employer", cascade="all, delete-orphan"
    )


class EmployerMember(Base):
    """Recruiters / staff belonging to an employer org."""

    __tablename__ = "employer_members"
    __table_args__ = (
        UniqueConstraint("employer_id", "user_id", name="uq_employer_member"),
    )

    member_id: Mapped[str] = uuid_pk()
    employer_id: Mapped[str] = uuid_fk("employers.employer_id")
    user_id: Mapped[str] = uuid_fk("users.user_id")
    member_role: Mapped[str] = mapped_column(String(50), default="recruiter")  # owner|admin|recruiter
    created_at: Mapped[datetime] = created_col()

    employer: Mapped[Employer] = relationship(back_populates="members")


class TeamInvite(Base):
    """A pending invitation to join an employer's team.

    Lets an owner invite anyone by email — the invitee need not have an account
    yet. They accept via a link that carries the opaque token (only its hash is
    stored), sign up or sign in, and join with the assigned role.
    """

    __tablename__ = "team_invites"

    invite_id: Mapped[str] = uuid_pk()
    employer_id: Mapped[str] = uuid_fk("employers.employer_id")
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(50), default="recruiter")  # admin|recruiter
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending|accepted|revoked
    invited_by_user_id: Mapped[Optional[str]] = uuid_fk("users.user_id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created_col()
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


class JobPosting(Base):
    __tablename__ = "job_postings"

    job_id: Mapped[str] = uuid_pk()
    employer_id: Mapped[str] = uuid_fk("employers.employer_id")
    posted_by_user_id: Mapped[Optional[str]] = uuid_fk("users.user_id", nullable=True, ondelete="SET NULL")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    specialty: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    profession_type: Mapped[Optional[str]] = mapped_column(String(50), index=True)
    job_type: Mapped[JobType] = mapped_column(Enum(JobType), default=JobType.travel, index=True)
    shift_type: Mapped[Optional[str]] = mapped_column(String(50), index=True)  # days|nights|evenings|rotating
    pay_rate_min: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), index=True)
    pay_rate_max: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), index=True)
    pay_unit: Mapped[str] = mapped_column(String(20), default="hourly")  # hourly|annual|weekly
    housing_stipend: Mapped[Optional[float]] = mapped_column(Numeric(10, 2))
    signing_bonus: Mapped[Optional[float]] = mapped_column(Numeric(10, 2))
    city: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    state_code: Mapped[Optional[str]] = mapped_column(String(2), index=True)
    lat: Mapped[Optional[float]] = mapped_column(Float)
    lng: Mapped[Optional[float]] = mapped_column(Float)
    # Parsed out of the imported description: the end client the req is for,
    # the staffing agency, and the ATS requisition code (unique per opening).
    facility: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    agency: Mapped[Optional[str]] = mapped_column(String(150))
    req_code: Mapped[Optional[str]] = mapped_column(String(60), index=True)
    # Provenance for jobs pulled from an external ATS (e.g. LaborEdge Nexus).
    # (source, external_id) is the idempotency key the sync upserts against.
    external_source: Mapped[Optional[str]] = mapped_column(String(30), index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(60), index=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    requirements: Mapped[dict] = mapped_column(JSON, default=dict)
    benefits: Mapped[list] = mapped_column(JSON, default=list)  # health|dental|401k|housing
    required_skills: Mapped[list] = mapped_column(JSON, default=list)
    required_certifications: Mapped[list] = mapped_column(JSON, default=list)
    years_exp_min: Mapped[Optional[int]] = mapped_column(SmallInteger)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.active, index=True)
    is_urgent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    application_count: Mapped[int] = mapped_column(Integer, default=0)
    search_text: Mapped[Optional[str]] = mapped_column(Text, index=True)
    start_date: Mapped[Optional[date]] = mapped_column(Date)
    expires_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime, index=True)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()

    employer: Mapped[Employer] = relationship(back_populates="jobs")
    applications: Mapped[list["Application"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    def rebuild_search_text(self) -> None:
        parts = [self.title, self.specialty, self.profession_type, self.city,
                 self.state_code, self.description]
        skills = self.required_skills or []
        text = " ".join(str(p) for p in [*parts, *skills] if p).lower()
        # search_text carries a plain btree index, and Postgres caps a btree
        # entry at ~2704 bytes — some imported job descriptions run past that and
        # would fail the INSERT. The useful search tokens (title/specialty/city/
        # state) lead the string, so trimming the tail keeps search working.
        encoded = text.encode("utf-8")
        if len(encoded) > 2400:
            text = encoded[:2400].decode("utf-8", "ignore")
        self.search_text = text


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("job_id", "profile_id", name="uq_application_job_profile"),
    )

    application_id: Mapped[str] = uuid_pk()
    job_id: Mapped[str] = uuid_fk("job_postings.job_id")
    profile_id: Mapped[str] = uuid_fk("profiles.profile_id")
    status: Mapped[ApplicationStatus] = mapped_column(
        Enum(ApplicationStatus), default=ApplicationStatus.applied, index=True
    )
    cover_letter: Mapped[Optional[str]] = mapped_column(Text)
    resume_snapshot_url: Mapped[Optional[str]] = mapped_column(Text)
    match_score: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), index=True)
    recruiter_notes: Mapped[Optional[str]] = mapped_column(Text)
    recruiter_rating: Mapped[Optional[int]] = mapped_column(SmallInteger)
    source: Mapped[str] = mapped_column(String(50), default="platform", index=True)
    applied_at: Mapped[datetime] = created_col()
    status_updated_at: Mapped[datetime] = updated_col()

    job: Mapped[JobPosting] = relationship(back_populates="applications")
    events: Mapped[list["ApplicationEvent"]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )


class ApplicationEvent(Base):
    """ATS stage-change history for an application."""

    __tablename__ = "application_events"

    event_id: Mapped[str] = uuid_pk()
    application_id: Mapped[str] = uuid_fk("applications.application_id")
    from_status: Mapped[Optional[str]] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30))
    note: Mapped[Optional[str]] = mapped_column(Text)
    actor_user_id: Mapped[Optional[str]] = uuid_fk("users.user_id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = created_col()

    application: Mapped[Application] = relationship(back_populates="events")


class SavedJob(Base):
    __tablename__ = "saved_jobs"
    __table_args__ = (
        UniqueConstraint("profile_id", "job_id", name="uq_saved_job"),
    )

    save_id: Mapped[str] = uuid_pk()
    profile_id: Mapped[str] = uuid_fk("profiles.profile_id")
    job_id: Mapped[str] = uuid_fk("job_postings.job_id")
    saved_at: Mapped[datetime] = created_col()
