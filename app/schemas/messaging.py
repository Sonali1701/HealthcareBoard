"""Messaging, notification, interview and offer schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from ..models.enums import (
    InterviewStatus,
    MessageKind,
    NotificationType,
    OfferStatus,
)
from .common import ORMModel


# --- Threads & messages ---------------------------------------------------

class ThreadCreate(BaseModel):
    """Address a thread either by user id or by profile id (the profile must
    belong to a registered user — sourced résumés have no account to message)."""
    recipient_id: Optional[str] = None
    profile_id: Optional[str] = None
    job_id: Optional[str] = None
    body: Optional[str] = None


class MessageCreate(BaseModel):
    body: Optional[str] = None
    kind: MessageKind = MessageKind.text
    payload: dict = Field(default_factory=dict)


class MessageOut(ORMModel):
    message_id: str
    thread_id: str
    sender_id: str
    recipient_id: str
    kind: MessageKind
    body: Optional[str] = None
    payload: dict
    is_read: bool
    read_at: Optional[datetime] = None
    created_at: datetime


class ThreadOut(ORMModel):
    thread_id: str
    participant_a_id: str
    participant_b_id: str
    job_id: Optional[str] = None
    ats_stage: str
    last_message_at: Optional[datetime] = None
    created_at: datetime


class ThreadSummary(ThreadOut):
    """A thread as it appears in the inbox list — resolved for the *viewer*, so
    the client never has to look up who the other participant is."""
    other_name: str = "Unknown"
    other_role: Optional[str] = None
    other_initials: str = "?"
    last_message: Optional[str] = None
    last_message_kind: Optional[MessageKind] = None
    last_sender_is_me: bool = False
    unread_count: int = 0


class ThreadDetail(ThreadSummary):
    messages: list[MessageOut] = Field(default_factory=list)
    # A long thread is loaded newest-last in pages; `has_more` tells the client
    # there is older history above what it holds.
    total_messages: int = 0
    has_more: bool = False


class ATSStageUpdate(BaseModel):
    ats_stage: str


# --- Notifications --------------------------------------------------------

class NotificationOut(ORMModel):
    notification_id: str
    type: NotificationType
    title: str
    body: Optional[str] = None
    data: dict
    is_read: bool
    created_at: datetime


# --- Interviews -----------------------------------------------------------

class InterviewCreate(BaseModel):
    profile_id: str
    job_id: Optional[str] = None
    thread_id: Optional[str] = None
    proposed_slots: list[datetime] = Field(default_factory=list)
    location: Optional[str] = None
    notes: Optional[str] = None


class InterviewConfirm(BaseModel):
    confirmed_slot: datetime


class InterviewOut(ORMModel):
    interview_id: str
    thread_id: Optional[str] = None
    job_id: Optional[str] = None
    profile_id: str
    recruiter_user_id: str
    status: InterviewStatus
    proposed_slots: list
    confirmed_slot: Optional[datetime] = None
    location: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime


# --- Offers ---------------------------------------------------------------

class OfferCreate(BaseModel):
    job_id: str
    profile_id: str
    thread_id: Optional[str] = None
    pay_rate: Optional[float] = None
    pay_unit: str = "hourly"
    start_date: Optional[datetime] = None
    details: dict = Field(default_factory=dict)
    expires_at: Optional[datetime] = None


class OfferRespond(BaseModel):
    status: OfferStatus  # accepted | declined


class OfferOut(ORMModel):
    offer_id: str
    job_id: str
    profile_id: str
    thread_id: Optional[str] = None
    recruiter_user_id: str
    status: OfferStatus
    pay_rate: Optional[float] = None
    pay_unit: str
    start_date: Optional[datetime] = None
    details: dict
    expires_at: Optional[datetime] = None
    created_at: datetime
