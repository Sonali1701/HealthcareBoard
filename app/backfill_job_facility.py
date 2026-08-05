"""Parse facility / agency / requisition code out of imported job descriptions.

The imported reqs pack their real metadata into one description line:

    Facility / End Client: Genesis - Mid-America & SE Region · Client: CareerStaff
    · Bill rate: $24.5/hr · Recruitment Manager: Radixsol · Ceipal Job Code: JPC - 317762

Pulling those into columns makes the board readable (30 openings at one facility
collapse into one row) and lets recruiters match candidates who have already
worked at the end client.

Run:  python -m app.backfill_job_facility [--dry-run]
"""
from __future__ import annotations

import argparse
import re

from sqlalchemy import func, select

from .database import SessionLocal
from .models import JobPosting

# Fields are separated by a middot/bullet; a value runs to the next label.
_FACILITY = re.compile(r"Facility\s*/?\s*(?:End\s*Client)?\s*:\s*(.+?)(?=\s*[·•|]|\s{2,}|$)", re.I)
_AGENCY = re.compile(r"\bClient\s*:\s*(.+?)(?=\s*[·•|]|\s{2,}|$)", re.I)
_REQ = re.compile(r"Job\s*Code\s*:\s*(.+?)(?=\s*[·•|]|\s{2,}|$)", re.I)


def _clean(v: str | None, limit: int) -> str | None:
    if not v:
        return None
    # The import mangled some separators into replacement chars; strip them.
    v = re.sub(r"[�·•]+", " ", v)
    v = re.sub(r"\s+", " ", v).strip(" -:–")
    return v[:limit] or None


def run(dry_run: bool = False) -> None:
    db = SessionLocal()
    try:
        jobs = db.scalars(select(JobPosting)).all()
        fac = ag = rq = 0
        for j in jobs:
            text_ = j.description or ""
            if not text_:
                continue
            if not j.facility:
                m = _FACILITY.search(text_)
                val = _clean(m.group(1) if m else None, 200)
                if val:
                    j.facility, fac = val, fac + 1
            if not j.agency:
                # "Facility / End Client:" also contains "Client:", so search
                # after the facility segment to avoid matching the wrong label.
                tail = text_.split("End Client:", 1)[-1]
                m = _AGENCY.search(tail)
                val = _clean(m.group(1) if m else None, 150)
                if val:
                    j.agency, ag = val, ag + 1
            if not j.req_code:
                m = _REQ.search(text_)
                val = _clean(m.group(1) if m else None, 60)
                if val:
                    j.req_code, rq = val, rq + 1

        if dry_run:
            db.rollback()
            print(f"DRY RUN — would set facility={fac} agency={ag} req_code={rq} "
                  f"across {len(jobs)} jobs")
            return
        db.commit()
        print(f"jobs={len(jobs)}  facility set={fac}  agency set={ag}  req_code set={rq}")

        print("\ntop facilities:")
        for name, n in db.execute(
            select(JobPosting.facility, func.count())
            .where(JobPosting.facility.isnot(None))
            .group_by(JobPosting.facility).order_by(func.count().desc()).limit(8)
        ).all():
            print(f"  {n:4d}  {name}")
        dupes = db.execute(
            select(func.count()).select_from(
                select(JobPosting.req_code).where(JobPosting.req_code.isnot(None))
                .group_by(JobPosting.req_code).having(func.count() > 1).subquery())
        ).scalar()
        print(f"\nreq codes appearing more than once (true duplicates): {dupes}")
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    run(dry_run=ap.parse_args().dry_run)
