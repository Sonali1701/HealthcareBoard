"""Fetch jobs from the Ceipal Custom Report and upsert them into the job board.

  Inspect the raw report shape (safe, writes nothing):
      python -m app.importers.ceipal_jobs --inspect
  Sync jobs into the database:
      python -m app.importers.ceipal_jobs

Imported jobs live under a "Ceipal Imported Jobs" employer and are de-duplicated
by their Ceipal id (kept in requirements["ceipal_id"]), so re-running updates
rather than duplicates. The full raw row is stored in requirements["ceipal_raw"]
so no data is lost while we refine the field mapping.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys

from sqlalchemy import select

from ..database import SessionLocal, utcnow
from ..models import Employer, EmployerMember, JobPosting, User
from ..models.enums import JobStatus, JobType, UserRole, UserStatus
from ..security import hash_password
from ..services import ceipal


# --- helpers --------------------------------------------------------------

def _records(payload):
    """Pull the list of rows out of whatever shape the report returns."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("data", "results", "rows", "report_data", "reportData",
                  "records", "Data", "result"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
        for v in payload.values():          # else: first list value found
            if isinstance(v, list):
                return v
    return []


_EMPTY = {"", "null", "none", "n/a", "na", "-", "[]", "[, , ]"}

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}


def _pick(rec: dict, *names, default=None):
    """Case-insensitive lookup across several possible column names."""
    low = {str(k).strip().lower(): v for k, v in rec.items()}
    for n in names:
        v = low.get(n.strip().lower())
        if v is not None and str(v).strip().lower() not in _EMPTY:
            return v
    return default


def _money(v):
    """Extract the numeric rate from strings like '44', 'USD/44', '$1,200/wk'."""
    if v is None:
        return None
    nums = re.findall(r"\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(nums[-1]) if nums else None


def _parse_location(loc, states_full=None):
    """'[Wyncote, PA, 19095]' -> ('Wyncote', 'PA'); falls back to States name."""
    city = state = None
    if loc:
        parts = [p.strip() for p in str(loc).strip().strip("[]").split(",") if p.strip()]
        if parts:
            city = parts[0]
        if len(parts) >= 2 and len(parts[1]) == 2 and parts[1].isalpha():
            state = parts[1].upper()
    if not state and states_full:
        state = US_STATES.get(str(states_full).strip().lower())
    return city, state


def _system_employer(db) -> Employer:
    u = db.scalar(select(User).where(User.email == "ceipal-import@system.local"))
    if not u:
        u = User(email="ceipal-import@system.local",
                 password_hash=hash_password(secrets.token_hex(16)),
                 role=UserRole.admin, status=UserStatus.active, email_verified_at=utcnow())
        db.add(u)
        db.flush()
    emp = db.scalar(select(Employer).where(Employer.owner_user_id == u.user_id))
    if not emp:
        emp = Employer(owner_user_id=u.user_id, org_name="Ceipal Imported Jobs",
                       org_type="agency", is_verified=True)
        db.add(emp)
        db.flush()
        db.add(EmployerMember(employer_id=emp.employer_id, user_id=u.user_id, member_role="owner"))
    return emp


def _map_status(raw: str) -> JobStatus:
    s = (raw or "").lower()
    if any(x in s for x in ("close", "fill", "cancel", "hold", "inactive", "lost")):
        return JobStatus.closed
    return JobStatus.active


def _map_type(raw: str, duration: str = "") -> JobType:
    s = (raw or "").lower()
    for t in JobType:
        if t.value.replace("_", " ") in s or t.value in s:
            return t
    if "perm" in s or "direct" in s or "full" in s:
        return JobType.staff
    if "diem" in s or "prn" in s:
        return JobType.per_diem
    if "travel" in s:
        return JobType.travel
    # JobType is often blank in the report; agency bill-rate roles default to contract.
    return JobType.contract


def _map_job(rec: dict, employer: Employer):
    title = _pick(rec, "JobTitle", "Job Title", "title")
    if not title:
        return None
    code = str(_pick(rec, "JobCode", "Job Code", "id") or "").strip()
    city, state = _parse_location(_pick(rec, "Location"), _pick(rec, "States", "State"))
    pay = _money(_pick(rec, "PayRate/Salary", "PayRate", "Pay Rate"))
    bill = _money(_pick(rec, "BillRate", "ClientBillRate/Salary", "Bill Rate"))
    nums = sorted(n for n in (pay, bill) if n is not None)
    pmin = nums[0] if nums else None
    pmax = nums[-1] if nums else None

    client = _pick(rec, "Client")
    end_client = _pick(rec, "EndClient", "End Client")
    duration = _pick(rec, "Duration")
    rm = _pick(rec, "RecruitmentManager", "Recruitment Manager")
    pr = _pick(rec, "PrimaryRecruiter", "Primary Recruiter")

    parts = []
    if end_client:
        parts.append(f"Facility / End Client: {end_client}")
    if client:
        parts.append(f"Client: {client}")
    if duration:
        parts.append(f"Duration: {duration}")
    if bill:
        parts.append(f"Bill rate: ${bill:g}/hr")
    if rm:
        parts.append(f"Recruitment Manager: {rm}")
    if pr:
        parts.append(f"Primary Recruiter: {pr}")
    if code:
        parts.append(f"Ceipal Job Code: {code}")
    description = "  ·  ".join(parts) or None

    reqs = {
        "source": "ceipal",
        "ceipal_id": code or None,
        "job_code": code or None,
        "client": client,
        "end_client": end_client,
        "duration": duration,
        "states": _pick(rec, "States", "State"),
        "recruitment_manager": rm,
        "primary_recruiter": pr,
        "ceipal_raw": rec,
    }
    return {
        "ext": code or None,
        "reqs": reqs,
        "fields": dict(
            employer_id=employer.employer_id,
            title=str(title)[:300],
            job_type=_map_type(str(_pick(rec, "JobType", "Job Type") or ""), str(duration or "")),
            city=(city[:120] if city else None),
            state_code=(state[:2] if state else None),
            pay_rate_min=pmin,
            pay_rate_max=pmax,
            description=description,
            status=_map_status(str(_pick(rec, "JobStatus", "Job Status", "Status") or "")),
        ),
    }


# --- main -----------------------------------------------------------------

def run(inspect: bool = False) -> None:
    print("Authenticating with Ceipal…")
    token = ceipal.get_token()

    if inspect:
        print("Fetching first page…")
        payload = ceipal.fetch_report(token)
        rows = _records(payload)
        print(f"Page 1 returned {len(rows)} row(s); record_count={payload.get('record_count') if isinstance(payload, dict) else '?'}.")
        print("\n--- top-level type:", type(payload).__name__)
        if isinstance(payload, dict):
            print("--- top-level keys:", list(payload)[:20])
        if rows:
            print("--- first row columns:", list(rows[0])[:40])
            print("--- first row sample:")
            print(json.dumps(rows[0], indent=2, default=str)[:1500])
        else:
            print("--- raw payload sample:", json.dumps(payload, default=str)[:1000])
        print("\n(inspect only — nothing written.)")
        return

    print("Fetching all report pages…")
    rows = ceipal.fetch_all_records(token)
    print(f"Report returned {len(rows)} row(s).")

    db = SessionLocal()
    created = updated = skipped = 0
    try:
        employer = _system_employer(db)
        existing = {
            (j.requirements or {}).get("ceipal_id"): j
            for j in db.scalars(select(JobPosting).where(JobPosting.employer_id == employer.employer_id))
        }
        for rec in rows:
            mapped = _map_job(rec, employer)
            if not mapped:
                skipped += 1
                continue
            fields, ext, reqs = mapped["fields"], mapped["ext"], mapped["reqs"]
            job = existing.get(ext) if ext else None
            if job:
                for k, v in fields.items():
                    setattr(job, k, v)
                job.requirements = reqs
                updated += 1
            else:
                job = JobPosting(**fields, requirements=reqs)
                db.add(job)
                created += 1
                if ext:
                    existing[ext] = job  # collapse same-code rows within this run
            job.rebuild_search_text()
        db.commit()
        print(f"\nSynced: {created} created, {updated} updated, {skipped} skipped (no title).")
        print("Jobs are under the 'Ceipal Imported Jobs' employer and live on the board.")
        return {"rows": len(rows), "created": created, "updated": updated, "skipped": skipped}
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect", action="store_true",
                    help="print the report shape without writing anything")
    args = ap.parse_args()
    try:
        run(inspect=args.inspect)
        return 0
    except ceipal.CeipalError as e:
        print(f"\nCeipal error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
