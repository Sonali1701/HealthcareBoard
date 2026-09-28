"""Per-recruiter sender configuration retained for Halo compatibility."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base, created_col, updated_col, uuid_fk, uuid_pk


class MedhuntSmsSender(Base):
    __tablename__ = "medhunt_sms_senders"
    __table_args__ = (
        UniqueConstraint("employer_id", "user_id", name="uq_medhunt_sms_sender_org_user"),
        UniqueConstraint("employer_id", "sender_number", name="uq_medhunt_sms_sender_org_number"),
        Index("ix_medhunt_sms_senders_employer_id", "employer_id"),
        Index("ix_medhunt_sms_senders_user_id", "user_id"),
    )

    sender_id: Mapped[str] = uuid_pk()
    employer_id: Mapped[str] = uuid_fk("employers.employer_id")
    user_id: Mapped[str] = uuid_fk("users.user_id")
    sender_number: Mapped[str] = mapped_column(String(40), nullable=False)
    zoom_user_id: Mapped[str] = mapped_column(String(80), nullable=False)
    updated_by_user_id: Mapped[str] = uuid_fk("users.user_id", ondelete="RESTRICT")
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()
