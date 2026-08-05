"""Saved searches — a recruiter's standing sourcing criteria.

A recruiter who sources "ICU RNs in Texas" wants to know when *new* people match
it, not to re-run the same filters by hand every morning. The saved row keeps
the filter set plus the match count at the last check, so the delta since then
is what turns into a notification.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base, TZDateTime, created_col, updated_col, uuid_fk, uuid_pk


class SavedSearch(Base):
    __tablename__ = "saved_searches"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "name", name="uq_saved_search_owner_name"),
    )

    search_id: Mapped[str] = uuid_pk()
    owner_user_id: Mapped[str] = uuid_fk("users.user_id")
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # The directory filter set, stored exactly as the API accepts it.
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    notify: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Match count when this search was last checked — the baseline the next
    # check is compared against to find what is new.
    last_count: Mapped[Optional[int]] = mapped_column(Integer)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()
