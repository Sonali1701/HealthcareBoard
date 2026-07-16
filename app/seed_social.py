"""Seed candidate-facing demo data: a job-seeker account (the mockup's Jessica
persona) + community feed posts + notifications.

Run:  python -m app.seed_social
Login:  seeker@example.com / Password123!
"""
from __future__ import annotations

from sqlalchemy import func, select

from .database import SessionLocal, init_db, utcnow
from datetime import timedelta

from .models import (
    Certification,
    License,
    Message,
    MessageThread,
    Notification,
    Post,
    Profile,
    ProfileSkill,
    User,
)
from .models.enums import (
    LicenseStatus,
    MessageKind,
    NotificationType,
    ProfileSource,
    UserRole,
    UserStatus,
)
from .security import hash_password

SEEKER_EMAIL = "seeker@example.com"
PASSWORD = "Password123!"

POSTS = [
    ("Just wrapped a 13-week ICU travel contract — what an experience. Night-shift "
     "critical care pushes you, but the growth is unreal. Happy to answer questions "
     "from anyone starting their first travel assignment 👇",
     ["TravelNursing", "ICULife", "CriticalCare"], 284, 47),
    ("Quick tip for clinicians entering the ER: pre-chart your assessments and set "
     "your primary/secondary survey workflow before the shift. Cut my documentation "
     "time by ~35%.", ["ClinicalTips", "EmergencyMedicine"], 512, 83),
    ("Allergy & Immunology colleagues — what's your go-to approach for pediatric "
     "food-allergy introduction protocols this year? Comparing notes ahead of a talk.",
     ["AllergyImmunology", "Pediatrics"], 96, 21),
    ("Reminder: license renewal season. Compact (eNLC) states make multi-state travel "
     "so much smoother — worth verifying your eligibility early.",
     ["Licensing", "TravelNursing"], 140, 18),
]


def seed() -> None:
    init_db()
    db = SessionLocal()
    try:
        # --- Seeker account (Jessica persona) ---
        seeker = db.scalar(select(User).where(User.email == SEEKER_EMAIL))
        if not seeker:
            seeker = User(email=SEEKER_EMAIL, password_hash=hash_password(PASSWORD),
                          role=UserRole.job_seeker, status=UserStatus.active,
                          email_verified_at=utcnow())
            db.add(seeker)
            db.flush()
        jessica = db.scalar(select(Profile).where(Profile.user_id == seeker.user_id))
        if not jessica:
            jessica = Profile(
                user_id=seeker.user_id, first_name="Jessica", last_name="Martinez",
                headline="ICU Travel Nurse · CCRN · 8 yrs", specialty="ICU",
                profession_type="RN", years_experience=8, city="Houston", state_code="TX",
                pay_min_hourly=52, open_to_work=True, completion_score=88,
                job_type_prefs=["travel", "staff"], source=ProfileSource.signup,
                bio="Critical-care RN with 8 years of ICU experience across Level I trauma centers.",
            )
            jessica.rebuild_search_text()
            db.add(jessica)
            db.flush()
            for s in ["ICU", "Critical Care", "Ventilator", "ACLS"]:
                db.add(ProfileSkill(profile_id=jessica.profile_id, name=s, years=8))
            for c in ["CCRN", "ACLS", "BLS"]:
                db.add(Certification(profile_id=jessica.profile_id, cert_name=c, issuing_body="AHA"))
            db.add(License(profile_id=jessica.profile_id, license_type="RN",
                           license_number="TX-RN-204881", state_code="TX",
                           status=LicenseStatus.active, verified_at=utcnow(),
                           verification_source="nursys", is_compact=True))

        # --- Community feed posts (authored by Jessica + physicians) ---
        if (db.scalar(select(func.count()).select_from(Post)) or 0) == 0:
            authors = [jessica] + db.scalars(
                select(Profile).where(Profile.source == ProfileSource.resume_parse).limit(3)
            ).all()
            for i, (body, tags, likes, comments) in enumerate(POSTS):
                author = authors[i % len(authors)]
                db.add(Post(author_profile_id=author.profile_id, body=body, tags=tags,
                            like_count=likes, comment_count=comments))

        # --- Notifications for Jessica ---
        if not db.scalar(select(Notification).where(Notification.user_id == seeker.user_id)):
            db.add_all([
                Notification(user_id=seeker.user_id, type=NotificationType.job_match,
                             title="New job match", body="Travel ICU RN — Houston, TX matches your profile (94%)",
                             data={}),
                Notification(user_id=seeker.user_id, type=NotificationType.connection,
                             title="Connection request", body="Dr. Kevin Liang wants to connect", data={}),
                Notification(user_id=seeker.user_id, type=NotificationType.message,
                             title="New message", body="A recruiter sent you a message about an ICU role", data={}),
            ])

        # --- A conversation between a recruiter and Jessica ---
        recruiter = db.scalar(select(User).where(User.email == "recruiter@example.com"))
        if recruiter and not db.scalar(
            select(MessageThread).where(
                MessageThread.participant_a_id.in_([recruiter.user_id, seeker.user_id]),
                MessageThread.participant_b_id.in_([recruiter.user_id, seeker.user_id]),
            )
        ):
            thread = MessageThread(participant_a_id=recruiter.user_id,
                                   participant_b_id=seeker.user_id, ats_stage="in_conversation")
            db.add(thread)
            db.flush()
            base = utcnow() - timedelta(hours=2)
            convo = [
                (recruiter.user_id, "Hi Jessica! I'm Rachel from Memorial Hermann. Your ICU/CCRN background is a great fit for a 13-week travel contract opening Feb 1 — open to hearing more?"),
                (seeker.user_id, "Hi Rachel! Yes, definitely interested. What's the pay package look like?"),
                (recruiter.user_id, "$62/hr + $2,400/wk housing. I can send the full breakdown and the offer letter today."),
            ]
            for i, (sender, body) in enumerate(convo):
                recipient = seeker.user_id if sender == recruiter.user_id else recruiter.user_id
                db.add(Message(thread_id=thread.thread_id, sender_id=sender, recipient_id=recipient,
                               kind=MessageKind.text, body=body, is_read=True,
                               created_at=base + timedelta(minutes=i * 9)))
            thread.last_message_at = base + timedelta(minutes=len(convo) * 9)

        db.commit()
        print("Seeded candidate demo.")
        print(f"  Job-seeker login: {SEEKER_EMAIL} / {PASSWORD}  (Jessica Martinez, RN)")
        print(f"  {len(POSTS)} community posts, 3 notifications.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
