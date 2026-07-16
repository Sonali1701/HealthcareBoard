"""Remove all seeded/demo data, keeping only the REAL data:

  KEEP:
    - the "Ceipal Imported Jobs" employer, its owner (system) user, and every
      job synced from Ceipal;
    - every résumé-imported candidate profile (source=resume_parse) with its
      licenses / certifications / work history / skills, the user accounts that
      own them, and their files in R2.
  DELETE (everything else):
    - demo/test user accounts and their signup profiles;
    - all other employers and their demo jobs;
    - all applications, saved jobs, posts, comments, likes, connections,
      messages, threads, notifications, interviews, offers, match runs,
      pay packages, and dev tokens / audit logs (all seeded).

Preview (safe, writes nothing):   python -m app.purge_demo
Apply:                            python -m app.purge_demo --yes
"""
from __future__ import annotations

import sys

from sqlalchemy import delete, func, select

from .database import SessionLocal
from .models import (
    Application,
    ApplicationEvent,
    AuditLog,
    Connection,
    EmailVerificationToken,
    Employer,
    EmployerMember,
    Interview,
    JobPosting,
    MatchResult,
    MatchRun,
    Message,
    MessageThread,
    Notification,
    Offer,
    PasswordResetToken,
    PayPackage,
    Post,
    PostComment,
    PostLike,
    Profile,
    SavedJob,
    Session,
    User,
)
from .models.enums import ProfileSource

KEEP_EMPLOYER_NAME = "Ceipal Imported Jobs"

# Activity / content / token tables that are entirely seeded — wiped in full.
# Ordered children-before-parents so foreign keys never block the delete.
WIPE_IN_ORDER = [
    ApplicationEvent,
    Application,
    SavedJob,
    Offer,
    Interview,
    Message,
    MessageThread,
    PostLike,
    PostComment,
    Post,
    Connection,
    Notification,
    MatchResult,
    MatchRun,
    PayPackage,
    EmailVerificationToken,
    PasswordResetToken,
    AuditLog,
    Session,
]


def main() -> None:
    apply = "--yes" in sys.argv
    db = SessionLocal()
    try:
        def n(model, *where):
            q = select(func.count()).select_from(model)
            for c in where:
                q = q.where(c)
            return db.scalar(q) or 0

        # --- What to KEEP -------------------------------------------------
        keep_emps = db.scalars(
            select(Employer).where(Employer.org_name == KEEP_EMPLOYER_NAME)
        ).all()
        if not keep_emps:
            print(f"ABORT: employer {KEEP_EMPLOYER_NAME!r} not found. Run the Ceipal "
                  "sync first — refusing to delete without the real jobs present.")
            return
        keep_emp_ids = [e.employer_id for e in keep_emps]

        real_profiles = db.scalars(
            select(Profile).where(Profile.source == ProfileSource.resume_parse)
        ).all()
        keep_profile_ids = {p.profile_id for p in real_profiles}

        # Keep only the system user(s) that own the Ceipal employer. Every other
        # account is seeded — including the @candidate.demo logins that happen to
        # own real résumé profiles, which we detach (below) before deleting.
        keep_user_ids = {e.owner_user_id for e in keep_emps if e.owner_user_id}

        # --- What to DELETE ----------------------------------------------
        del_jobs = n(JobPosting, JobPosting.employer_id.notin_(keep_emp_ids))
        keep_jobs = n(JobPosting, JobPosting.employer_id.in_(keep_emp_ids))
        del_emps = n(Employer, Employer.employer_id.notin_(keep_emp_ids))
        demo_profiles = db.scalars(
            select(Profile).where(Profile.profile_id.notin_(keep_profile_ids))
        ).all() if keep_profile_ids else db.scalars(select(Profile)).all()
        demo_users = db.scalars(
            select(User).where(User.user_id.notin_(keep_user_ids))
        ).all() if keep_user_ids else db.scalars(select(User)).all()
        demo_user_ids = {u.user_id for u in demo_users}
        detach = [p for p in real_profiles if p.user_id in demo_user_ids]

        print("Will KEEP (real data):")
        print(f"  Ceipal jobs              : {keep_jobs}")
        print(f"  résumé candidate profiles: {len(real_profiles)} "
              f"({len(detach)} detached from demo logins, preserved)")
        print(f"  user accounts            : {len(keep_user_ids)} (Ceipal system user)")
        print()
        print("Will DELETE (demo/seeded):")
        print(f"  other employers          : {del_emps}")
        print(f"  their jobs               : {del_jobs}")
        print(f"  demo/signup profiles     : {len(demo_profiles)}")
        print(f"  demo user accounts       : {len(demo_users)}")
        print(f"  applications             : {n(Application)}")
        print(f"  posts                    : {n(Post)}")
        print(f"  messages / threads       : {n(Message)} / {n(MessageThread)}")
        print(f"  notifications            : {n(Notification)}")

        if not apply:
            print("\n(preview only — re-run with  --yes  to apply)")
            return

        # --- Apply, all in one transaction -------------------------------
        for p in detach:
            p.user_id = None      # preserve the real profile, drop the demo login
        db.flush()
        for model in WIPE_IN_ORDER:
            db.execute(delete(model))
        db.execute(delete(JobPosting).where(JobPosting.employer_id.notin_(keep_emp_ids)))
        db.execute(delete(EmployerMember).where(EmployerMember.employer_id.notin_(keep_emp_ids)))
        db.execute(delete(Employer).where(Employer.employer_id.notin_(keep_emp_ids)))
        for p in demo_profiles:
            db.delete(p)          # ORM cascade -> licenses / certs / work / skills
        for u in demo_users:
            db.delete(u)          # ORM cascade -> sessions / oauth
        db.commit()

        print(f"\nPurged. Board now shows only real data: "
              f"{keep_jobs} Ceipal jobs + {len(real_profiles)} candidate profiles.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
