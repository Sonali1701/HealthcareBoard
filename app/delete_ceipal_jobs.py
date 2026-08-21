"""Delete the old CEIPAL-imported jobs (the "Ceipal Imported Jobs" employer).

Preview first (writes nothing):
    python -m app.delete_ceipal_jobs

Actually delete:
    python -m app.delete_ceipal_jobs --confirm

Only jobs under the "Ceipal Imported Jobs" employer are touched. Dependent rows
that would block the delete (saved jobs, offers, applications and their events)
are removed first; the remaining foreign keys (pools, submissions, threads,
interviews, analytics) are ON DELETE SET NULL and clear themselves. Live Nexus
jobs and everything else are left untouched.
"""
from __future__ import annotations

import sys

from sqlalchemy import delete, func, select

from .database import SessionLocal
from .models import (
    Application,
    ApplicationEvent,
    Employer,
    JobPosting,
    Offer,
    SavedJob,
)

EMPLOYER_NAME = "Ceipal Imported Jobs"


def run(confirm: bool = False) -> dict:
    db = SessionLocal()
    try:
        emp = db.scalar(select(Employer).where(Employer.org_name == EMPLOYER_NAME))
        if not emp:
            print(f'No "{EMPLOYER_NAME}" employer found — nothing to delete.')
            return {"jobs": 0}

        job_ids = select(JobPosting.job_id).where(JobPosting.employer_id == emp.employer_id)
        n_jobs = db.scalar(select(func.count()).select_from(job_ids.subquery())) or 0
        n_apps = db.scalar(select(func.count()).select_from(Application)
                           .where(Application.job_id.in_(job_ids))) or 0
        n_saved = db.scalar(select(func.count()).select_from(SavedJob)
                            .where(SavedJob.job_id.in_(job_ids))) or 0
        n_offers = db.scalar(select(func.count()).select_from(Offer)
                             .where(Offer.job_id.in_(job_ids))) or 0

        print(f'"{EMPLOYER_NAME}" employer: {n_jobs} job(s)')
        print(f'  dependent rows — applications: {n_apps}, saved: {n_saved}, offers: {n_offers}')

        if not confirm:
            print("\nDRY RUN — nothing was deleted. Re-run with --confirm to delete.")
            return {"jobs": n_jobs, "deleted": False}

        # Clear blocking dependents, then the jobs. SET NULL FKs auto-clear.
        app_ids = select(Application.application_id).where(Application.job_id.in_(job_ids))
        db.execute(delete(ApplicationEvent).where(ApplicationEvent.application_id.in_(app_ids)))
        db.execute(delete(Application).where(Application.job_id.in_(job_ids)))
        db.execute(delete(SavedJob).where(SavedJob.job_id.in_(job_ids)))
        db.execute(delete(Offer).where(Offer.job_id.in_(job_ids)))
        db.execute(delete(JobPosting).where(JobPosting.employer_id == emp.employer_id))
        db.commit()

        remaining = db.scalar(select(func.count()).select_from(JobPosting)) or 0
        print(f"\nDeleted {n_jobs} CEIPAL job(s). {remaining} job(s) remain on the board.")
        return {"jobs": n_jobs, "deleted": True, "remaining": remaining}
    finally:
        db.close()


def main() -> int:
    run(confirm="--confirm" in sys.argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
