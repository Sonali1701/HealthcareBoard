"""Recruiter outreach — templates, campaigns and per-candidate sends.

Two rules are baked into the schema rather than left to the UI:

* A campaign send is recorded per candidate, so open/reply state belongs to a
  row rather than to a counter that can drift.
* Every send carries an unsubscribe token. Bulk email to people who never
  signed up is exactly the case where opt-out has to be built in from the
  start, not retrofitted.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base, TZDateTime, created_col, updated_col, uuid_fk, uuid_pk

# Placeholders a recruiter can use in a subject or body.
MERGE_FIELDS = ("first_name", "last_name", "full_name", "specialty",
                "profession_type", "city", "state_code", "years_experience")

SEND_STATUSES = ("queued", "sent", "failed", "skipped", "opened", "replied", "unsubscribed")


class EmailTemplate(Base):
    __tablename__ = "email_templates"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "name", name="uq_email_template_owner_name"),
    )

    template_id: Mapped[str] = uuid_pk()
    owner_user_id: Mapped[str] = uuid_fk("users.user_id")
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()


class OutreachCampaign(Base):
    __tablename__ = "outreach_campaigns"

    campaign_id: Mapped[str] = uuid_pk()
    owner_user_id: Mapped[str] = uuid_fk("users.user_id")
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    pool_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("talent_pools.pool_id", ondelete="SET NULL"), index=True
    )
    template_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("email_templates.template_id", ondelete="SET NULL")
    )
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    opened: Mapped[int] = mapped_column(Integer, default=0)
    replied: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()

    messages: Mapped[list["OutreachMessage"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class OutreachMessage(Base):
    __tablename__ = "outreach_messages"

    message_id: Mapped[str] = uuid_pk()
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("outreach_campaigns.campaign_id", ondelete="CASCADE"),
        index=True, nullable=False,
    )
    profile_id: Mapped[str] = uuid_fk("profiles.profile_id")
    to_email: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    subject: Mapped[Optional[str]] = mapped_column(String(300))
    body: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    reason: Mapped[Optional[str]] = mapped_column(String(80))   # why it was skipped/failed
    # Opaque per-send token: powers the tracking pixel and the opt-out link
    # without exposing the profile id in a URL people can share.
    token: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    opened_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    replied_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = created_col()

    campaign: Mapped[OutreachCampaign] = relationship(back_populates="messages")


class Suppression(Base):
    """Do-not-contact list. One row per email address, global to the platform."""

    __tablename__ = "outreach_suppressions"

    suppression_id: Mapped[str] = uuid_pk()
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    reason: Mapped[str] = mapped_column(String(60), default="unsubscribed")
    created_at: Mapped[datetime] = created_col()
