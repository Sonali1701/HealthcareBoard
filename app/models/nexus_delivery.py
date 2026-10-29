"""Durable delivery queue for contacts sent to an organization's Nexus ATS."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base, TZDateTime, created_col, updated_col, uuid_fk, uuid_pk


class NexusContactDelivery(Base):
    __tablename__ = "nexus_contact_deliveries"
    __table_args__ = (
        UniqueConstraint("integration_id", "profile_id", name="uq_nexus_delivery_profile"),
        Index("ix_nexus_delivery_ready", "status", "next_attempt_at"),
    )

    delivery_id: Mapped[str] = uuid_pk()
    integration_id: Mapped[str] = uuid_fk("ats_organization_integrations.integration_id")
    employer_id: Mapped[str] = uuid_fk("employers.employer_id")
    profile_id: Mapped[Optional[str]] = uuid_fk(
        "profiles.profile_id", nullable=True, ondelete="SET NULL"
    )
    actor_user_id: Mapped[Optional[str]] = uuid_fk(
        "users.user_id", nullable=True, ondelete="SET NULL"
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    nexus_candidate_id: Mapped[Optional[str]] = mapped_column(String(100))
    completed_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()
