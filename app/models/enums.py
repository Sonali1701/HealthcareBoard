"""Enumerated types shared across the data model."""
from __future__ import annotations

import enum


class UserRole(str, enum.Enum):
    job_seeker = "job_seeker"
    employer = "employer"
    recruiter = "recruiter"
    admin = "admin"


class UserStatus(str, enum.Enum):
    active = "active"
    suspended = "suspended"
    deleted = "deleted"
    pending_verify = "pending_verify"


class LicenseStatus(str, enum.Enum):
    active = "active"
    expired = "expired"
    suspended = "suspended"
    pending = "pending"


class ProfileSource(str, enum.Enum):
    signup = "signup"
    json_import = "json_import"
    resume_parse = "resume_parse"
    manual = "manual"


class JobType(str, enum.Enum):
    travel = "travel"
    staff = "staff"
    per_diem = "per_diem"
    contract = "contract"


class JobStatus(str, enum.Enum):
    draft = "draft"
    active = "active"
    paused = "paused"
    closed = "closed"
    expired = "expired"


class ApplicationStatus(str, enum.Enum):
    applied = "applied"
    screening = "screening"
    interview = "interview"
    offer = "offer"
    hired = "hired"
    rejected = "rejected"
    withdrawn = "withdrawn"


class ConnectionStatus(str, enum.Enum):
    pending = "pending"
    accepted = "accepted"
    declined = "declined"
    blocked = "blocked"


class SubscriptionTier(str, enum.Enum):
    free = "free"
    basic = "basic"
    pro = "pro"
    enterprise = "enterprise"


class MessageKind(str, enum.Enum):
    text = "text"
    job_card = "job_card"
    schedule = "schedule"
    offer = "offer"
    system = "system"


class NotificationType(str, enum.Enum):
    message = "message"
    application = "application"
    connection = "connection"
    job_match = "job_match"
    system = "system"


class InterviewStatus(str, enum.Enum):
    proposed = "proposed"
    confirmed = "confirmed"
    completed = "completed"
    cancelled = "cancelled"


class OfferStatus(str, enum.Enum):
    sent = "sent"
    accepted = "accepted"
    declined = "declined"
    expired = "expired"
