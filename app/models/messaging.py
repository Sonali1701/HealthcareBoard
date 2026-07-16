"""Domain 5 — Messaging & Notifications: message_threads, messages,
notifications, interviews, offers."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Enum,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base, TZDateTime, created_col, updated_col, uuid_fk, uuid_pk
from .enums import InterviewStatus, MessageKind, NotificationType, OfferStatus


class MessageThread(Base):
    __tablename__ = "message_threads"

    thread_id: Mapped[str] = uuid_pk()
    # Two participants (users). Recruiter <-> candidate, etc.
    participant_a_id: Mapped[str] = uuid_fk("users.user_id")
    participant_b_id: Mapped[str] = uuid_fk("users.user_id")
    # Optional context: a job this conversation is about.
    job_id: Mapped[Optional[str]] = uuid_fk("job_postings.job_id", nullable=True, ondelete="SET NULL")
    # ATS pipeline stage for this conversation (chat-platform CRM).
    ats_stage: Mapped[str] = mapped_column(String(50), default="initial_contact")
    last_message_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime, index=True)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()

    messages: Mapped[list["Message"]] = relationship(
        back_populates="thread", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"

    message_id: Mapped[str] = uuid_pk()
    thread_id: Mapped[str] = uuid_fk("message_threads.thread_id")
    sender_id: Mapped[str] = uuid_fk("users.user_id")
    recipient_id: Mapped[str] = uuid_fk("users.user_id")
    kind: Mapped[MessageKind] = mapped_column(Enum(MessageKind), default=MessageKind.text)
    body: Mapped[Optional[str]] = mapped_column(Text)
    # Structured payload for job_card / schedule / offer messages.
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = created_col()

    thread: Mapped[MessageThread] = relationship(back_populates="messages")


class Notification(Base):
    __tablename__ = "notifications"

    notification_id: Mapped[str] = uuid_pk()
    user_id: Mapped[str] = uuid_fk("users.user_id")
    type: Mapped[NotificationType] = mapped_column(
        Enum(NotificationType), default=NotificationType.system, index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[Optional[str]] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = created_col()


class Interview(Base):
    __tablename__ = "interviews"

    interview_id: Mapped[str] = uuid_pk()
    thread_id: Mapped[Optional[str]] = uuid_fk("message_threads.thread_id", nullable=True, ondelete="SET NULL")
    job_id: Mapped[Optional[str]] = uuid_fk("job_postings.job_id", nullable=True, ondelete="SET NULL")
    profile_id: Mapped[str] = uuid_fk("profiles.profile_id")
    recruiter_user_id: Mapped[str] = uuid_fk("users.user_id")
    status: Mapped[InterviewStatus] = mapped_column(
        Enum(InterviewStatus), default=InterviewStatus.proposed, index=True
    )
    proposed_slots: Mapped[list] = mapped_column(JSON, default=list)  # ISO datetime strings
    confirmed_slot: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    location: Mapped[Optional[str]] = mapped_column(String(255))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = created_col()
    updated_at: Mapped[datetime] = updated_col()


class Offer(Base):
    __tablename__ = "offers"

    offer_id: Mapped[str] = uuid_pk()
    job_id: Mapped[str] = uuid_fk("job_postings.job_id")
    profile_id: Mapped[str] = uuid_fk("profiles.profile_id")
    thread_id: Mapped[Optional[str]] = uuid_fk("message_threads.thread_id", nullable=True, ondelete="SET NULL")
    recruiter_user_id: Mapped[str] = uuid_fk("users.user_id")
    status: Mapped[OfferStatus] = mapped_column(Enum(OfferStatus), default=OfferStatus.sent, index=True)
    pay_rate: Mapped[Optional[float]] = mapped_column(Numeric(10, 2))
    pay_unit: Mapped[str] = mapped_column(String(20), default="hourly")
    start_date: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    responded_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = created_col()
