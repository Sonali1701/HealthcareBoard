"""Queued contact searches for recovered candidate and license records."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base, created_col, updated_col, uuid_fk, uuid_pk


class RecoveredDataContactLookupJob(Base):
    __tablename__ = "recovered_data_contact_lookup_jobs"
    __table_args__ = (
        Index("ix_recovered_lookup_queue", "status", "created_at"),
        Index("ix_recovered_lookup_owner_record", "requester_user_id", "source_record_key"),
    )

    job_id: Mapped[str] = uuid_pk()
    requester_user_id: Mapped[str] = uuid_fk("users.user_id")
    source_record_key: Mapped[str] = mapped_column(String(60), nullable=False)
    lookup_name: Mapped[str] = mapped_column(String(240), nullable=False)
    lookup_location: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    found: Mapped[bool | None] = mapped_column(Boolean)
    source: Mapped[str | None] = mapped_column(String(40))
    detail: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(80))
    credits_charged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    credits_remaining: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()
