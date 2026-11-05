"""Durable queue for slow external provider-contact searches."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base, created_col, updated_col, uuid_fk, uuid_pk


class ContactLookupJob(Base):
    __tablename__ = "contact_lookup_jobs"
    __table_args__ = (
        Index("ix_contact_lookup_jobs_queue", "status", "created_at"),
        Index("ix_contact_lookup_jobs_owner_profile", "requester_user_id", "profile_id"),
    )

    job_id: Mapped[str] = uuid_pk()
    requester_user_id: Mapped[str] = uuid_fk("users.user_id")
    profile_id: Mapped[str] = uuid_fk("profiles.profile_id")
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    found: Mapped[bool | None] = mapped_column(Boolean)
    source: Mapped[str | None] = mapped_column(String(40))
    detail: Mapped[str | None] = mapped_column(Text)
    credits_charged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    credits_remaining: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()
