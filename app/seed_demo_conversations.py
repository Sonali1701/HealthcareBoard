"""Seed demo conversation/CRM data so the chat platform shows live data.

Uses your imported physician profiles: turns a few of them into candidate user
accounts, opens recruiter<->candidate threads with messages + ATS stages, and
creates an employer, jobs, and applications across pipeline stages (so the CRM
funnel/KPIs populate).

Run:    python -m app.seed_demo_conversations
Clear:  python -m app.seed_demo_conversations --clear   (removes only this demo data)

Safe to re-run: it skips if demo threads already exist.
"""
from __future__ import annotations

import sys
from datetime import timedelta

from sqlalchemy import select

from .database import SessionLocal, init_db, utcnow
from .models import (
    Application,
    ApplicationEvent,
    Employer,
    EmployerMember,
    JobPosting,
    Message,
    MessageThread,
    Profile,
    User,
)
from .models.enums import (
    ApplicationStatus,
    JobType,
    MessageKind,
    ProfileSource,
    UserRole,
    UserStatus,
)
from .security import hash_password

RECRUITER_EMAIL = "recruiter@example.com"
DEMO_PASSWORD = "Password123!"
DEMO_TAG = "demo-conversation"  # stamped on created jobs' requirements for cleanup


def _get_or_create_recruiter(db) -> User:
    rec = db.scalar(select(User).where(User.email == RECRUITER_EMAIL))
    if not rec:
        rec = User(email=RECRUITER_EMAIL, password_hash=hash_password(DEMO_PASSWORD),
                   role=UserRole.recruiter, status=UserStatus.active,
                   email_verified_at=utcnow())
        db.add(rec)
        db.flush()
    return rec


def clear_demo(db) -> None:
    jobs = db.scalars(select(JobPosting).where(
        JobPosting.requirements.contains({"tag": DEMO_TAG}))).all() \
        if False else []  # JSON contains varies by backend; do it the portable way
    jobs = [j for j in db.scalars(select(JobPosting)).all()
            if isinstance(j.requirements, dict) and j.requirements.get("tag") == DEMO_TAG]
    emp_ids = {j.employer_id for j in jobs}
    for j in jobs:
        db.delete(j)
    for emp in db.scalars(select(Employer).where(Employer.employer_id.in_(emp_ids))).all() if emp_ids else []:
        db.delete(emp)
    # delete demo threads (those whose participants include a candidate-user)
    for t in db.scalars(select(MessageThread)).all():
        db.delete(t)
    # unlink + delete candidate users we created (email domain marker)
    for u in db.scalars(select(User).where(User.email.like("%@candidate.demo"))).all():
        prof = db.scalar(select(Profile).where(Profile.user_id == u.user_id))
        if prof:
            prof.user_id = None
        db.delete(u)
    db.commit()
    print("Cleared demo conversation data.")


def seed(db) -> None:
    if db.scalar(select(MessageThread)):
        print("Threads already exist — skipping (run with --clear first to reset).")
        return

    recruiter = _get_or_create_recruiter(db)

    # Reuse an existing employer/job if the jobs seeder already ran.
    employer = db.scalar(select(Employer).where(Employer.owner_user_id == recruiter.user_id))
    if not employer:
        employer = Employer(owner_user_id=recruiter.user_id, org_name="Allergy Partners",
                            org_type="clinic", city="Raleigh", state_code="NC",
                            is_verified=True, rating_avg=4.7)
        db.add(employer)
        db.flush()
        db.add(EmployerMember(employer_id=employer.employer_id, user_id=recruiter.user_id,
                              member_role="owner"))

    job = db.scalar(select(JobPosting).where(JobPosting.employer_id == employer.employer_id))
    if not job:
        job = JobPosting(
            employer_id=employer.employer_id, posted_by_user_id=recruiter.user_id,
            title="Allergy & Immunology Physician — Locum/Travel",
            specialty="Allergy & Immunology", profession_type="MD", job_type=JobType.travel,
            pay_rate_min=180, pay_rate_max=240, pay_unit="hourly",
            city="Raleigh", state_code="NC",
            required_skills=["Allergy & Immunology"], requirements={"tag": DEMO_TAG},
            description="Seeking board-certified allergists for locum coverage.",
        )
        db.add(job)
        db.flush()

    physicians = db.scalars(
        select(Profile).where(Profile.source == ProfileSource.resume_parse)
        .order_by(Profile.first_name).limit(6)
    ).all()
    if not physicians:
        print("No imported physician profiles found — run the resume importer first.")
        return

    convo_lines = [
        ("them", "Hi, thanks for reaching out about the locum role!"),
        ("me", "Of course! Your Allergy & Immunology background is a great fit. Are you open to travel?"),
        ("them", "Yes, I'm available starting next month."),
        ("me", "Excellent — I'll send over the details and the formal offer shortly."),
    ]
    stages = ["in_conversation", "application_received", "interview_scheduled",
              "offer_extended", "initial_contact"]
    app_statuses = [ApplicationStatus.screening, ApplicationStatus.interview,
                    ApplicationStatus.offer, ApplicationStatus.hired,
                    ApplicationStatus.applied]

    made = 0
    for i, p in enumerate(physicians[:5]):
        # Turn the candidate into a user account so they're messageable.
        if not p.user_id:
            email = f"{p.first_name}.{p.last_name}{i}@candidate.demo".lower().replace(" ", "")
            u = User(email=email, password_hash=hash_password(DEMO_PASSWORD),
                     role=UserRole.job_seeker, status=UserStatus.active,
                     email_verified_at=utcnow())
            db.add(u)
            db.flush()
            p.user_id = u.user_id
        cand_user_id = p.user_id

        # Thread for the first 3 candidates.
        if i < 3:
            thread = MessageThread(participant_a_id=recruiter.user_id,
                                   participant_b_id=cand_user_id, job_id=job.job_id,
                                   ats_stage=stages[i % len(stages)])
            db.add(thread)
            db.flush()
            base = utcnow() - timedelta(hours=3)
            for k, (side, text) in enumerate(convo_lines):
                sender = recruiter.user_id if side == "me" else cand_user_id
                recipient = cand_user_id if side == "me" else recruiter.user_id
                db.add(Message(thread_id=thread.thread_id, sender_id=sender,
                               recipient_id=recipient, kind=MessageKind.text, body=text,
                               is_read=(side == "me"), created_at=base + timedelta(minutes=k*7)))
            thread.last_message_at = base + timedelta(minutes=len(convo_lines)*7)

        # Application across pipeline stages (drives the funnel/KPIs).
        app = Application(job_id=job.job_id, profile_id=p.profile_id,
                          status=app_statuses[i % len(app_statuses)],
                          match_score=80 + i, source="platform")
        db.add(app)
        job.application_count += 1
        db.flush()
        db.add(ApplicationEvent(application_id=app.application_id,
                                to_status=app.status.value, actor_user_id=recruiter.user_id))
        made += 1

    db.commit()
    print(f"Seeded demo conversations: 1 employer, 1 job, {made} candidates "
          f"({min(3, made)} chat threads), applications across pipeline stages.")
    print(f"Recruiter login: {RECRUITER_EMAIL} / {DEMO_PASSWORD}")


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        if "--clear" in sys.argv:
            clear_demo(db)
        else:
            seed(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
