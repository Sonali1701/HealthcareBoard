"""Durable state for overnight contact enrichment of Halo provider profiles."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base, TZDateTime, created_col, updated_col


class ProfileContactBackfill(Base):
    __tablename__ = "profile_contact_backfills"

    profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("profiles.profile_id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    medhunt_candidate_id: Mapped[Optional[int]] = mapped_column(Integer)
    run_id: Mapped[Optional[str]] = mapped_column(String(80), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime, index=True)
    next_eligible_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime, index=True)
    enriched_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime, index=True)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()
