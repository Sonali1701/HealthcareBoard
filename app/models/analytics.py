"""Domain 6 — Matching, Pay packages, Audit: match_runs, match_results,
pay_packages, audit_logs."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base, created_col, uuid_fk, uuid_pk


class MatchRun(Base):
    """A single execution of the AI matching engine for a job spec."""

    __tablename__ = "match_runs"

    run_id: Mapped[str] = uuid_pk()
    job_id: Mapped[Optional[str]] = uuid_fk("job_postings.job_id", nullable=True, ondelete="SET NULL")
    requested_by_user_id: Mapped[Optional[str]] = uuid_fk("users.user_id", nullable=True, ondelete="SET NULL")
    job_spec: Mapped[dict] = mapped_column(JSON, default=dict)
    weights: Mapped[dict] = mapped_column(JSON, default=dict)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_score: Mapped[Optional[float]] = mapped_column(Numeric(5, 2))
    created_at: Mapped[datetime] = created_col()

    results: Mapped[list["MatchResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class MatchResult(Base):
    __tablename__ = "match_results"

    result_id: Mapped[str] = uuid_pk()
    run_id: Mapped[str] = uuid_fk("match_runs.run_id")
    profile_id: Mapped[str] = uuid_fk("profiles.profile_id")
    rank: Mapped[int] = mapped_column(Integer)
    score_total: Mapped[float] = mapped_column(Numeric(5, 2))
    score_skills: Mapped[Optional[float]] = mapped_column(Numeric(5, 2))
    score_experience: Mapped[Optional[float]] = mapped_column(Numeric(5, 2))
    score_location: Mapped[Optional[float]] = mapped_column(Numeric(5, 2))
    score_pay: Mapped[Optional[float]] = mapped_column(Numeric(5, 2))
    match_reason: Mapped[Optional[str]] = mapped_column(Text)
    shortlisted: Mapped[bool] = mapped_column(default=False)

    run: Mapped[MatchRun] = relationship(back_populates="results")


class PayPackage(Base):
    """A saved travel-nurse pay package computed by the GSA calculator."""

    __tablename__ = "pay_packages"

    package_id: Mapped[str] = uuid_pk()
    created_by_user_id: Mapped[Optional[str]] = uuid_fk("users.user_id", nullable=True, ondelete="SET NULL")
    profile_id: Mapped[Optional[str]] = uuid_fk("profiles.profile_id", nullable=True, ondelete="SET NULL")
    job_id: Mapped[Optional[str]] = uuid_fk("job_postings.job_id", nullable=True, ondelete="SET NULL")
    label: Mapped[Optional[str]] = mapped_column(String(200))
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = created_col()


class AuditLog(Base):
    __tablename__ = "audit_logs"

    log_id: Mapped[str] = uuid_pk()
    actor_user_id: Mapped[Optional[str]] = uuid_fk("users.user_id", nullable=True, ondelete="SET NULL")
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(100))
    entity_id: Mapped[Optional[str]] = mapped_column(String(36))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = created_col()
