"""Seed a few realistic job postings so the board isn't empty.

Run:  python -m app.seed_jobs
Idempotent-ish: skips if more than 3 jobs already exist.
"""
from __future__ import annotations

from sqlalchemy import func, select

from .database import SessionLocal, init_db, utcnow
from .models import Employer, EmployerMember, JobPosting, User
from .models.enums import JobStatus, JobType, UserRole, UserStatus
from .security import hash_password

JOBS = [
    {"title": "Allergy & Immunology Physician — Locum", "specialty": "Allergy & Immunology",
     "profession_type": "MD", "job_type": JobType.travel, "city": "Raleigh", "state_code": "NC",
     "pay_rate_min": 180, "pay_rate_max": 240, "is_featured": True,
     "required_skills": ["Allergy & Immunology", "Asthma"], "shift_type": "days"},
    {"title": "Staff Allergist — Outpatient Clinic", "specialty": "Allergy & Immunology",
     "profession_type": "MD", "job_type": JobType.staff, "city": "Dallas", "state_code": "TX",
     "pay_rate_min": 210000, "pay_rate_max": 280000, "pay_unit": "annual",
     "required_skills": ["Allergy & Immunology"], "shift_type": "days"},
    {"title": "Pediatric Allergist", "specialty": "Allergy & Immunology",
     "profession_type": "MD", "job_type": JobType.staff, "city": "Chicago", "state_code": "IL",
     "pay_rate_min": 200000, "pay_rate_max": 260000, "pay_unit": "annual", "is_urgent": True,
     "required_skills": ["Pediatric Asthma", "Allergy & Immunology"], "shift_type": "days"},
    {"title": "Immunology Physician — Travel Contract", "specialty": "Allergy & Immunology",
     "profession_type": "DO", "job_type": JobType.travel, "city": "Phoenix", "state_code": "AZ",
     "pay_rate_min": 175, "pay_rate_max": 230, "required_skills": ["Allergy & Immunology"],
     "shift_type": "days"},
    {"title": "Allergy & Asthma Specialist", "specialty": "Allergy & Immunology",
     "profession_type": "MD", "job_type": JobType.per_diem, "city": "Los Angeles", "state_code": "CA",
     "pay_rate_min": 190, "pay_rate_max": 250, "required_skills": ["Asthma", "Allergy & Immunology"],
     "shift_type": "days"},
    {"title": "Locum Allergist — Northeast Coverage", "specialty": "Allergy & Immunology",
     "profession_type": "MD", "job_type": JobType.travel, "city": "Boston", "state_code": "MA",
     "pay_rate_min": 200, "pay_rate_max": 260, "is_featured": True,
     "required_skills": ["Allergy & Immunology"], "shift_type": "days"},
]


def seed() -> None:
    init_db()
    db = SessionLocal()
    try:
        if (db.scalar(select(func.count()).select_from(JobPosting)) or 0) > 3:
            print("Jobs already seeded — skipping.")
            return
        recruiter = db.scalar(select(User).where(User.email == "recruiter@example.com"))
        if not recruiter:
            recruiter = User(email="recruiter@example.com", password_hash=hash_password("Password123!"),
                             role=UserRole.recruiter, status=UserStatus.active, email_verified_at=utcnow())
            db.add(recruiter); db.flush()
        employer = db.scalar(select(Employer).where(Employer.owner_user_id == recruiter.user_id))
        if not employer:
            employer = Employer(owner_user_id=recruiter.user_id, org_name="Allergy Partners",
                                org_type="clinic", city="Raleigh", state_code="NC", is_verified=True)
            db.add(employer); db.flush()
            db.add(EmployerMember(employer_id=employer.employer_id, user_id=recruiter.user_id, member_role="owner"))

        n = 0
        for j in JOBS:
            job = JobPosting(employer_id=employer.employer_id, posted_by_user_id=recruiter.user_id,
                             status=JobStatus.active, pay_unit=j.pop("pay_unit", "hourly"), **j)
            job.rebuild_search_text()
            db.add(job)
            n += 1
        db.commit()
        print(f"Seeded {n} jobs for {employer.org_name}.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
