"""Organization-owned Zoom OAuth connection."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base, TZDateTime, created_col, updated_col, uuid_fk, uuid_pk


class ZoomOrganizationIntegration(Base):
    __tablename__ = "zoom_organization_integrations"
    __table_args__ = (
        UniqueConstraint("employer_id", name="uq_zoom_organization_employer"),
        Index("ix_zoom_organization_account", "zoom_account_id"),
    )

    integration_id: Mapped[str] = uuid_pk()
    employer_id: Mapped[str] = uuid_fk("employers.employer_id")
    zoom_account_id: Mapped[Optional[str]] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="connected", server_default="connected")
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    scopes: Mapped[Optional[str]] = mapped_column(Text)
    connected_by_user_id: Mapped[Optional[str]] = uuid_fk("users.user_id", nullable=True, ondelete="SET NULL")
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()
