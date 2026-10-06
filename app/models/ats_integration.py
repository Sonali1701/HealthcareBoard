"""Encrypted organization-owned ATS connections."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base, TZDateTime, created_col, updated_col, uuid_fk, uuid_pk


class AtsOrganizationIntegration(Base):
    __tablename__ = "ats_organization_integrations"
    __table_args__ = (
        UniqueConstraint("employer_id", "provider", name="uq_ats_organization_provider"),
        Index("ix_ats_organization_employer", "employer_id"),
    )

    integration_id: Mapped[str] = uuid_pk()
    employer_id: Mapped[str] = uuid_fk("employers.employer_id")
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="connected", server_default="connected"
    )
    settings_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    connected_by_user_id: Mapped[Optional[str]] = uuid_fk(
        "users.user_id", nullable=True, ondelete="SET NULL"
    )
    last_tested_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()
