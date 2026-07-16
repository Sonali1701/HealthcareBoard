"""SQLAlchemy ORM models.

Importing this package registers every table on ``Base.metadata`` so that
``Base.metadata.create_all()`` builds the full schema.

Table inventory (28):
  Auth:       users, oauth_accounts, sessions, password_reset_tokens,
              email_verification_tokens
  Profiles:   profiles, licenses, work_history, certifications, profile_skills
  Jobs:       employers, employer_members, job_postings, applications,
              application_events, saved_jobs
  Social:     connections, posts, post_likes, post_comments
  Messaging:  message_threads, messages, notifications, interviews, offers
  Analytics:  match_runs, match_results, pay_packages, audit_logs
"""
from .analytics import AuditLog, MatchResult, MatchRun, PayPackage
from .auth import (
    EmailVerificationToken,
    OAuthAccount,
    PasswordResetToken,
    Session,
    User,
)
from .enums import (
    ApplicationStatus,
    ConnectionStatus,
    InterviewStatus,
    JobStatus,
    JobType,
    LicenseStatus,
    MessageKind,
    NotificationType,
    OfferStatus,
    ProfileSource,
    SubscriptionTier,
    UserRole,
    UserStatus,
)
from .job import (
    Application,
    ApplicationEvent,
    Employer,
    EmployerMember,
    JobPosting,
    SavedJob,
)
from .messaging import Interview, Message, MessageThread, Notification, Offer
from .profile import Certification, License, Profile, ProfileSkill, WorkHistory
from .social import Connection, Post, PostComment, PostLike

__all__ = [
    # auth
    "User", "OAuthAccount", "Session", "PasswordResetToken", "EmailVerificationToken",
    # profiles
    "Profile", "License", "WorkHistory", "Certification", "ProfileSkill",
    # jobs
    "Employer", "EmployerMember", "JobPosting", "Application", "ApplicationEvent", "SavedJob",
    # social
    "Connection", "Post", "PostLike", "PostComment",
    # messaging
    "MessageThread", "Message", "Notification", "Interview", "Offer",
    # analytics
    "MatchRun", "MatchResult", "PayPackage", "AuditLog",
    # enums
    "UserRole", "UserStatus", "LicenseStatus", "ProfileSource", "JobType", "JobStatus",
    "ApplicationStatus", "ConnectionStatus", "SubscriptionTier", "MessageKind",
    "NotificationType", "InterviewStatus", "OfferStatus",
]
