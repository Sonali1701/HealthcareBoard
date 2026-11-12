"""Contact lookup jobs for NPI directory rows that are not app profiles."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base, created_col, updated_col, uuid_fk, uuid_pk


class ProviderDirectoryContactLookupJob(Base):
    __tablename__ = "provider_directory_contact_lookup_jobs"
    __table_args__ = (
        Index("ix_directory_contact_lookup_queue", "status", "created_at"),
        Index("ix_directory_contact_lookup_owner_npi", "requester_user_id", "npi", "created_at"),
    )

    job_id: Mapped[str] = uuid_pk()
    requester_user_id: Mapped[str] = uuid_fk("users.user_id")
    npi: Mapped[str] = mapped_column(String(10), nullable=False)
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
