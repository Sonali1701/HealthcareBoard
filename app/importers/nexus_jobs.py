"""Fetch open jobs from LaborEdge Nexus and upsert them into the job board.

  Inspect the raw feed (safe, writes nothing):
      python -m app.importers.nexus_jobs --inspect
  Sync jobs into the database:
      python -m app.importers.nexus_jobs

Imported jobs live under a "LaborEdge Nexus" employer and are de-duplicated by
(external_source, external_id), so re-running updates rather than duplicates.
Requires the Nexus credentials in the environment (NEXUS_ENABLED=true,
NEXUS_USERNAME, NEXUS_PASSWORD, NEXUS_ORG_CODE).
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from itertools import islice

from sqlalchemy import select

from ..database import SessionLocal, utcnow
from ..models import Employer, EmployerMember, JobPosting, User
from ..models.enums import UserRole, UserStatus
from ..security import hash_password
from ..services.nexus import EXTERNAL_SOURCE, NexusClient, NexusError, map_job


def _system_employer(db) -> Employer:
    u = db.scalar(select(User).where(User.email == "nexus-import@system.local"))
    if not u:
        u = User(email="nexus-import@system.local",
                 password_hash=hash_password(secrets.token_hex(16)),
                 role=UserRole.admin, status=UserStatus.active, email_verified_at=utcnow())
        db.add(u)
        db.flush()
    emp = db.scalar(select(Employer).where(Employer.owner_user_id == u.user_id))
    if not emp:
        emp = Employer(owner_user_id=u.user_id, org_name="LaborEdge Nexus",
                       org_type="agency", is_verified=True)
        db.add(emp)
        db.flush()
        db.add(EmployerMember(employer_id=emp.employer_id, user_id=u.user_id, member_role="owner"))
    return emp


def run(inspect: bool = False, extra_filters: dict | None = None,
        limit: int | None = None):
    print("Authenticating with Nexus…")
    with NexusClient() as client:
        if inspect:
            payload = client.search_jobs({"jobStatusCode": "OPEN", **(extra_filters or {})}, start=0)
            records = payload.get("records") or []
            print(f"Page 1 returned {len(records)} record(s); count={payload.get('count')}.")
            if records:
                print("--- first record keys:", list(records[0])[:40])
                print("--- first record sample:")
                print(json.dumps(records[0], indent=2, default=str)[:1800])
                print("\n--- mapped to JobPosting fields:")
                print(json.dumps(map_job(records[0]), indent=2, default=str)[:1500])
            print("\n(inspect only — nothing written.)")
            return {"count": payload.get("count"), "page1": len(records)}

        jobs = client.iter_open_jobs(extra_filters=extra_filters)
        if limit:
            print(f"Fetching up to {limit} open jobs…")
            records = list(islice(jobs, limit))
        else:
            print("Fetching all open jobs…")
            records = list(jobs)
    print(f"Nexus returned {len(records)} open job(s).")

    db = SessionLocal()
    created = updated = skipped = 0
    try:
        employer = _system_employer(db)
        existing = {
            j.external_id: j
            for j in db.scalars(
                select(JobPosting).where(
                    JobPosting.employer_id == employer.employer_id,
                    JobPosting.external_source == EXTERNAL_SOURCE,
                )
            )
        }
        for rec in records:
            fields = map_job(rec)
            if not fields:
                skipped += 1
                continue
            ext = fields["external_id"]
            job = existing.get(ext)
            if job:
                for k, v in fields.items():
                    setattr(job, k, v)
                updated += 1
            else:
                job = JobPosting(employer_id=employer.employer_id, **fields)
                db.add(job)
                created += 1
                existing[ext] = job  # collapse duplicate ids within this run
            job.rebuild_search_text()
        db.commit()
        print(f"\nSynced: {created} created, {updated} updated, {skipped} skipped.")
        print("Jobs are under the 'LaborEdge Nexus' employer and live on the board.")
        return {"records": len(records), "created": created, "updated": updated, "skipped": skipped}
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect", action="store_true",
                    help="print the feed shape and a mapped record without writing anything")
    ap.add_argument("--limit", type=int, default=None,
                    help="only import the first N open jobs (e.g. --limit 500)")
    args = ap.parse_args()
    try:
        run(inspect=args.inspect, limit=args.limit)
        return 0
    except NexusError as e:
        print(f"\nNexus error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
