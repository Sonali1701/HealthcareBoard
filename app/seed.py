"""Seed the database with demo data.

Run:  python -m app.seed
Creates users (job seekers + a recruiter), profiles with skills/licenses/certs,
an employer with active job postings, plus a couple of applications.

Login for the demo recruiter:  recruiter@healthboard.dev / Password123!
Login for a demo nurse:        jessica@healthboard.dev / Password123!
"""
from __future__ import annotations

from datetime import date, timedelta

from .database import SessionLocal, init_db, utcnow
from .models import (
    Application,
    ApplicationEvent,
    Certification,
    Employer,
    EmployerMember,
    JobPosting,
    License,
    Profile,
    ProfileSkill,
    User,
)
from .models.enums import (
    ApplicationStatus,
    JobType,
    LicenseStatus,
    ProfileSource,
    SubscriptionTier,
    UserRole,
    UserStatus,
)
from .security import hash_password

DEMO_PASSWORD = "Password123!"

NURSES = [
    {
        "email": "jessica@healthboard.dev", "first": "Jessica", "last": "Martinez",
        "specialty": "ICU", "profession": "RN", "years": 8, "city": "Houston",
        "state": "TX", "pay": 52.0, "headline": "ICU RN · CCRN · 8 yrs",
        "skills": ["ICU", "Ventilator", "ACLS", "Critical Care"],
        "certs": ["CCRN", "ACLS", "BLS"], "travel": True,
    },
    {
        "email": "alex@healthboard.dev", "first": "Alex", "last": "Moore",
        "specialty": "ER", "profession": "RN", "years": 5, "city": "Dallas",
        "state": "TX", "pay": 48.0, "headline": "ER RN · TNCC · 5 yrs",
        "skills": ["ER", "Trauma", "Triage", "ACLS"],
        "certs": ["TNCC", "ACLS", "BLS"], "travel": True,
    },
    {
        "email": "priya@healthboard.dev", "first": "Priya", "last": "Patel",
        "specialty": "OR", "profession": "RN", "years": 12, "city": "Austin",
        "state": "TX", "pay": 58.0, "headline": "OR Circulator · 12 yrs",
        "skills": ["OR", "Perioperative", "Sterile Technique"],
        "certs": ["CNOR", "BLS"], "travel": False,
    },
    {
        "email": "marcus@healthboard.dev", "first": "Marcus", "last": "Johnson",
        "specialty": "ICU", "profession": "RN", "years": 3, "city": "Los Angeles",
        "state": "CA", "pay": 55.0, "headline": "ICU RN · 3 yrs",
        "skills": ["ICU", "Critical Care", "ACLS"],
        "certs": ["ACLS", "BLS"], "travel": True,
    },
    {
        "email": "sara@healthboard.dev", "first": "Sara", "last": "Nguyen",
        "specialty": "Labor & Delivery", "profession": "RN", "years": 7,
        "city": "Phoenix", "state": "AZ", "pay": 50.0,
        "headline": "L&D RN · 7 yrs", "skills": ["Labor & Delivery", "Fetal Monitoring"],
        "certs": ["NRP", "BLS"], "travel": True,
    },
]


def seed() -> None:
    init_db()
    db = SessionLocal()
    try:
        if db.query(User).count() > 0:
            print("Database already has users — skipping seed. "
                  "(Delete healthboard.db to re-seed.)")
            return

        # --- Recruiter + employer ---
        recruiter = User(
            email="recruiter@healthboard.dev",
            password_hash=hash_password(DEMO_PASSWORD),
            role=UserRole.recruiter,
            status=UserStatus.active,
            email_verified_at=utcnow(),
        )
        db.add(recruiter)
        db.flush()

        employer = Employer(
            owner_user_id=recruiter.user_id,
            org_name="Memorial Health System",
            org_type="health_system",
            city="Houston", state_code="TX", bed_count=850,
            is_verified=True, rating_avg=4.6,
            subscription_tier=SubscriptionTier.pro, job_credits_balance=50,
        )
        db.add(employer)
        db.flush()
        db.add(EmployerMember(employer_id=employer.employer_id,
                              user_id=recruiter.user_id, member_role="owner"))

        # --- Job postings ---
        jobs = [
            JobPosting(
                employer_id=employer.employer_id, posted_by_user_id=recruiter.user_id,
                title="Travel ICU RN — 13 Week Contract", specialty="ICU",
                profession_type="RN", job_type=JobType.travel, shift_type="nights",
                pay_rate_min=48, pay_rate_max=62, pay_unit="hourly",
                housing_stipend=1400, signing_bonus=2000,
                city="Houston", state_code="TX",
                description="Level I trauma center seeking experienced ICU travelers.",
                required_skills=["ICU", "Ventilator", "Critical Care"],
                required_certifications=["CCRN", "ACLS", "BLS"],
                benefits=["health", "dental", "401k", "housing"],
                years_exp_min=2, is_urgent=True, is_featured=True,
                start_date=date.today() + timedelta(days=21),
            ),
            JobPosting(
                employer_id=employer.employer_id, posted_by_user_id=recruiter.user_id,
                title="ER RN — Staff Position", specialty="ER",
                profession_type="RN", job_type=JobType.staff, shift_type="rotating",
                pay_rate_min=40, pay_rate_max=55, pay_unit="hourly",
                city="Dallas", state_code="TX",
                description="Busy ER seeking permanent staff RNs.",
                required_skills=["ER", "Trauma", "Triage"],
                required_certifications=["TNCC", "ACLS", "BLS"],
                benefits=["health", "dental", "401k"],
                years_exp_min=1,
            ),
        ]
        db.add_all(jobs)
        db.flush()

        # --- Nurses + profiles ---
        profiles: list[Profile] = []
        for n in NURSES:
            u = User(
                email=n["email"], password_hash=hash_password(DEMO_PASSWORD),
                role=UserRole.job_seeker, status=UserStatus.active,
                email_verified_at=utcnow(),
            )
            db.add(u)
            db.flush()
            p = Profile(
                user_id=u.user_id, first_name=n["first"], last_name=n["last"],
                headline=n["headline"], specialty=n["specialty"],
                profession_type=n["profession"], years_experience=n["years"],
                city=n["city"], state_code=n["state"], pay_min_hourly=n["pay"],
                open_to_work=True, source=ProfileSource.json_import,
                job_type_prefs=["travel", "staff"] if n["travel"] else ["staff"],
                available_date=date.today() + timedelta(days=14),
            )
            p.rebuild_search_text()
            db.add(p)
            db.flush()
            for s in n["skills"]:
                db.add(ProfileSkill(profile_id=p.profile_id, name=s, years=n["years"]))
            for c in n["certs"]:
                db.add(Certification(profile_id=p.profile_id, cert_name=c,
                                     issuing_body="AHA"))
            # Reasonable completion score for seeded profiles.
            p.completion_score = 85
            db.add(License(
                profile_id=p.profile_id, license_type=n["profession"],
                license_number=f"{n['state']}-{1000 + len(profiles)}",
                state_code=n["state"], status=LicenseStatus.active,
                verified_at=utcnow(), verification_source="nursys", is_compact=True,
                expiry_date=date.today() + timedelta(days=365),
            ))
            profiles.append(p)

        # --- A couple of applications to the ICU job ---
        icu_job = jobs[0]
        for p in profiles[:2]:
            app = Application(
                job_id=icu_job.job_id, profile_id=p.profile_id,
                status=ApplicationStatus.applied, source="platform",
                match_score=88.5,
            )
            db.add(app)
            icu_job.application_count += 1
            db.flush()
            db.add(ApplicationEvent(application_id=app.application_id,
                                    to_status=ApplicationStatus.applied.value))

        db.commit()
        print("Seed complete.")
        print(f"  Recruiter: recruiter@healthboard.dev / {DEMO_PASSWORD}")
        print(f"  Nurse:     jessica@healthboard.dev / {DEMO_PASSWORD}")
        print(f"  {len(NURSES)} nurses, 1 employer, {len(jobs)} jobs created.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
