"""Candidate submissions to client facilities.

A talent pool records who an agency is *considering*; a submission records who
they actually put forward to a client, which is the billable event and the one
recruiters are measured on. Keeping it separate means a pool stays a working
shortlist rather than doubling as a system of record.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base, TZDateTime, created_col, uuid_fk, uuid_pk

SUBMISSION_STATUSES = ("submitted", "client_review", "interviewing",
                       "offered", "placed", "rejected", "withdrawn")


class Submission(Base):
    __tablename__ = "submissions"

    submission_id: Mapped[str] = uuid_pk()
    profile_id: Mapped[str] = uuid_fk("profiles.profile_id")
    job_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("job_postings.job_id", ondelete="SET NULL"), index=True)
    pool_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("talent_pools.pool_id", ondelete="SET NULL"))
    employer_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("employers.employer_id", ondelete="SET NULL"))
    # Denormalised from the job so a submission still reads correctly after the
    # requisition is closed and removed.
    facility: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    submitted_by_user_id: Mapped[str] = uuid_fk("users.user_id")
    status: Mapped[str] = mapped_column(String(24), default="submitted", index=True)
    bill_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    pay_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    note: Mapped[Optional[str]] = mapped_column(Text)
    submitted_at: Mapped[datetime] = created_col()
    status_updated_at: Mapped[datetime] = created_col()
    created_at: Mapped[datetime] = created_col()
