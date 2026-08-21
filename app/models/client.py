"""Client facilities — the hospitals and agencies a staffing team places into.

Until now a submission recorded its destination as a free-text ``facility``
string, so the same hospital was typed a dozen different ways and there was
nowhere to keep its contact or default bill rate. A Client is that facility as a
first-class record: job orders and submissions point at it, and it is scoped to
the agency (the owner plus their team) like pools and submissions are.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base, created_col, updated_col, uuid_fk, uuid_pk


class Client(Base):
    __tablename__ = "clients"

    client_id: Mapped[str] = uuid_pk()
    owner_user_id: Mapped[str] = uuid_fk("users.user_id")
    # The agency org, so the whole team shares the client list.
    employer_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("employers.employer_id", ondelete="SET NULL"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    facility_type: Mapped[Optional[str]] = mapped_column(String(80))
    city: Mapped[Optional[str]] = mapped_column(String(120))
    state_code: Mapped[Optional[str]] = mapped_column(String(2), index=True)
    website_url: Mapped[Optional[str]] = mapped_column(String(255))
    contact_name: Mapped[Optional[str]] = mapped_column(String(120))
    contact_email: Mapped[Optional[str]] = mapped_column(String(255))
    contact_phone: Mapped[Optional[str]] = mapped_column(String(40))
    default_bill_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()
