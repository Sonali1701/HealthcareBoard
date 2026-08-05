"""Talent pools — a recruiter's saved shortlists of sourced candidates.

The provider directory holds ~160k imported résumés that mostly have no platform
account, so the working unit of sourcing is not a chat thread but a *shortlist*:
the recruiter collects candidates for a role, annotates them, moves them through
a pipeline stage, and exports the pool for outreach.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base, created_col, updated_col, uuid_fk, uuid_pk

# Pipeline stages a shortlisted candidate moves through.
POOL_STAGES = ("sourced", "contacted", "screening", "submitted", "hired", "rejected")


class TalentPool(Base):
    __tablename__ = "talent_pools"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "name", name="uq_pool_owner_name"),
    )

    pool_id: Mapped[str] = uuid_pk()
    owner_user_id: Mapped[str] = uuid_fk("users.user_id")
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text)
    # Optional role this pool is sourcing for.
    job_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("job_postings.job_id", ondelete="SET NULL"), index=True
    )
    color: Mapped[str] = mapped_column(String(16), default="blue")
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()

    members: Mapped[list["TalentPoolMember"]] = relationship(
        back_populates="pool", cascade="all, delete-orphan"
    )


class TalentPoolMember(Base):
    __tablename__ = "talent_pool_members"
    __table_args__ = (
        UniqueConstraint("pool_id", "profile_id", name="uq_pool_profile"),
    )

    member_id: Mapped[str] = uuid_pk()
    pool_id: Mapped[str] = mapped_column(
        ForeignKey("talent_pools.pool_id", ondelete="CASCADE"), index=True, nullable=False
    )
    profile_id: Mapped[str] = uuid_fk("profiles.profile_id")
    stage: Mapped[str] = mapped_column(String(20), default="sourced", index=True)
    note: Mapped[Optional[str]] = mapped_column(Text)
    added_by_user_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("users.user_id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()

    pool: Mapped[TalentPool] = relationship(back_populates="members")
