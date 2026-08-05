"""Enrich profiles with STRUCTURED data extracted from their résumés.

The directory holds ~160k profiles but the structured tables behind them are
nearly empty: 0 work-history rows, a handful of licenses, no compact-license
flags. Search, matching and the copilot can only ever be as good as that data.

This turns each résumé into real rows:
  • licenses      — type + state + IS_COMPACT (eNLC / multistate) detection
  • work_history  — employer, title, specialty, dates, location
  • profile       — years_experience / specialty backfilled when missing

    python -m app.enrich_profiles --limit 20            # try a small slice
    python -m app.enrich_profiles --budget 5            # stop near $5
    python -m app.enrich_profiles --only <profile_id>   # re-extract one
    python -m app.enrich_profiles --dry-run --limit 5   # read + LLM, no writes

Budget-capped and resumable (a manifest records every processed profile, so a
re-run continues where it stopped). Rows it creates are tagged
verification_source='resume_extraction', so a re-run replaces only its own rows
and never clobbers recruiter-entered or pre-existing data.

Run `python -m app.migrate_resume_sections` first if you also want résumé
sections; this script is independent of it.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from .clean_names_llm import _llm, _resume_text_from_url, cost_usd
from .config import settings
from .database import SessionLocal
from .models import License, Profile, WorkHistory
from .models.enums import LicenseStatus

SOURCE = "resume_extraction"   # tag so re-runs replace only our own rows

# Credentials we accept as a license type.
_LICENSE_TYPES = {"RN", "LPN", "LVN", "CNA", "NP", "CRNA", "CNM", "APRN", "MD",
                  "DO", "PA", "RT", "RRT", "PT", "PTA", "OT", "OTA", "PHARMD",
                  "DNP", "FNP", "PMHNP", "AGNP", "MSN", "BSN"}
# Types where a compact / multistate license actually exists (nurse & therapy
# compacts). We refuse is_compact for anything else, to kill LLM false positives.
_COMPACT_ELIGIBLE = {"RN", "LPN", "LVN", "APRN", "PT", "PTA", "OT", "OTA"}
_EMP_TYPES = {"staff", "travel", "per_diem", "per diem", "agency", "contract",
              "prn", "temporary"}
_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC","PR","GU","VI",
}

_SYSTEM = ("You extract structured, factual data from a healthcare "
           "professional's résumé. Respond with ONLY one JSON object, no prose.")
_INSTR = (
    "Return JSON exactly like:\n"
    '{"years_experience":null,"primary_specialty":null,'
    '"work_authorization":null,"available":null,'
    '"licenses":[{"type":"RN","state":"CA","is_compact":false,'
    '"number":null,"expires":null}],'
    '"education":[{"institution":null,"degree":null,"field":null,"year":null}],'
    '"work_history":[{"employer":null,"title":null,"specialty":null,'
    '"employment_type":null,"start":null,"end":null,"city":null,"state":null}]}\n\n'
    "Rules — never invent; use null / [] when unknown:\n"
    "- work_authorization: short status ONLY if stated — 'US Citizen', "
    "'Green Card', 'Permanent Resident', 'H-1B', 'TN', 'EAD', 'Authorized to "
    "work in US'. null otherwise.\n"
    "- available: a start availability ONLY if stated — 'immediately', 'ASAP', "
    "or a date/month (YYYY-MM). null otherwise.\n"
    "- education: degrees/schools. degree e.g. 'BSN','ADN','MSN','MD','DPT'. "
    "year = graduation year if stated.\n"
    "- licenses: professional licenses the person holds. type = credential code "
    "(RN, LPN, LVN, CNA, NP, CRNA, CNM, MD, DO, PA, RT, PT, OT, PharmD). "
    "state = the 2-letter US state the license is issued in. Omit a license that "
    "has no clear state.\n"
    "- is_compact = true ONLY when the résumé indicates a NURSING license that is "
    "a COMPACT / MULTISTATE / eNLC / NLC license (those exact ideas). A single "
    "state license, or wording like 'single state', is false.\n"
    "- number / expires only when clearly stated (expires as YYYY-MM).\n"
    "- work_history: employment entries, most recent first. employer and title "
    "are required (skip an entry missing both). employment_type one of "
    "staff/travel/per_diem/agency/contract when evident. dates as YYYY or "
    "YYYY-MM; end = 'present' if it is the current job.\n"
    "- years_experience: total years of professional experience, integer, if "
    "determinable.\n"
    "- primary_specialty: the main clinical specialty (e.g. ICU, ER, Med-Surg, "
    "L&D, OR, Telemetry).\n"
)


# --- parsing helpers -------------------------------------------------------

def _int(v, lo=0, hi=70):
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _state(v):
    s = str(v or "").strip().upper()
    return s if s in _US_STATES else None


def _pdate(v):
    """Lenient résumé date → date (first of the period), or None. 'present'→None."""
    s = str(v or "").strip().lower()
    if not s or s in {"present", "current", "now", "ongoing", "null"}:
        return None
    ym = re.search(r"(19|20)\d{2}", s)
    if not ym:
        return None
    year = int(ym.group(0))
    mon = 1
    mm = re.search(r"\b(0?[1-9]|1[0-2])[/-]", s)
    if mm:
        mon = int(mm.group(1))
    months = ("jan feb mar apr may jun jul aug sep oct nov dec")
    for i, name in enumerate(months.split(), start=1):
        if name in s:
            mon = i
            break
    try:
        return date(year, mon, 1)
    except ValueError:
        return None


def _clean(v, n):
    return str(v or "").strip()[:n] or None


def _normalise(raw: dict | None) -> dict | None:
    """Validate the model JSON into rows we can safely persist, or None."""
    if not isinstance(raw, dict):
        return None
    licenses = []
    for lic in raw.get("licenses") or []:
        if not isinstance(lic, dict):
            continue
        lt = str(lic.get("type") or "").strip().upper().replace(".", "")
        lt = "PharmD" if lt == "PHARMD" else lt
        st = _state(lic.get("state"))
        if lt.upper() not in _LICENSE_TYPES or not st:
            continue
        compact = bool(lic.get("is_compact")) and lt.upper() in _COMPACT_ELIGIBLE
        licenses.append({
            "type": lt, "state": st, "is_compact": compact,
            "number": _clean(lic.get("number"), 100) or "",
            "expiry": _pdate(lic.get("expires")),
        })
    # de-dupe licenses on (type, state)
    seen, uniq = set(), []
    for l in licenses:
        key = (l["type"].upper(), l["state"])
        if key not in seen:
            seen.add(key)
            uniq.append(l)
    licenses = uniq[:15]

    work = []
    for w in raw.get("work_history") or []:
        if not isinstance(w, dict):
            continue
        emp = _clean(w.get("employer"), 200)
        title = _clean(w.get("title"), 200)
        if not emp and not title:
            continue
        et = str(w.get("employment_type") or "").strip().lower().replace(" ", "_")
        work.append({
            "employer": emp or "—", "title": title or "—",
            "specialty": _clean(w.get("specialty"), 100),
            "employment_type": et if et in {e.replace(" ", "_") for e in _EMP_TYPES} else None,
            "start": _pdate(w.get("start")), "end": _pdate(w.get("end")),
            "city": _clean(w.get("city"), 120), "state": _state(w.get("state")),
        })
    work = work[:25]

    education = []
    for e in raw.get("education") or []:
        if not isinstance(e, dict):
            continue
        inst = _clean(e.get("institution"), 160)
        deg = _clean(e.get("degree"), 80)
        if not inst and not deg:
            continue
        education.append({"institution": inst, "degree": deg,
                          "field": _clean(e.get("field"), 100),
                          "year": _int(e.get("year"), lo=1950, hi=2035)})
    education = education[:10]

    # Availability: a concrete date, or "immediately/asap" → today.
    avail = str(raw.get("available") or "").strip().lower()
    available_date = None
    if avail in {"immediately", "asap", "now", "available"}:
        available_date = date.today()
    elif avail and avail != "null":
        available_date = _pdate(avail)

    result = {
        "licenses": licenses,
        "work_history": work,
        "education": education,
        "work_authorization": _clean(raw.get("work_authorization"), 80),
        "available_date": available_date,
        "years_experience": _int(raw.get("years_experience")),
        "specialty": _clean(raw.get("primary_specialty"), 100),
        "has_compact": any(l["is_compact"] for l in licenses),
    }
    if not (licenses or work or education or result["work_authorization"]
            or result["years_experience"] or result["specialty"]):
        return None
    return result


def _fetch(row: dict):
    text = _resume_text_from_url(row["resume_url"])
    if not text or not text.strip():
        return row, "NO_TEXT"
    return row, _llm(text, system=_SYSTEM, instr=_INSTR, max_chars=12000)


def _write(pending: list[dict]) -> None:
    """Persist a batch through a short-lived session (Neon drops idle conns)."""
    if not pending:
        return
    db = SessionLocal()
    try:
        for u in pending:
            pid, data = u["profile_id"], u["data"]
            # Replace only our own extracted rows; leave recruiter/manual data.
            db.execute(delete(License).where(
                License.profile_id == pid, License.verification_source == SOURCE))
            db.execute(delete(WorkHistory).where(
                WorkHistory.profile_id == pid,
                WorkHistory.description == SOURCE))
            for l in data["licenses"]:
                db.add(License(
                    profile_id=pid, license_type=l["type"], license_number=l["number"],
                    state_code=l["state"], status=LicenseStatus.active,
                    expiry_date=l["expiry"], is_compact=l["is_compact"],
                    verification_source=SOURCE))
            for w in data["work_history"]:
                db.add(WorkHistory(
                    profile_id=pid, employer_name=w["employer"], job_title=w["title"],
                    specialty=w["specialty"], employment_type=w["employment_type"],
                    start_date=w["start"], end_date=w["end"],
                    city=w["city"], state_code=w["state"], description=SOURCE))
            # Backfill profile scalars only when they're empty/zero.
            prof = db.get(Profile, pid)
            if prof:
                if data["years_experience"] and not prof.years_experience:
                    prof.years_experience = data["years_experience"]
                if data["specialty"] and not (prof.specialty or "").strip():
                    prof.specialty = data["specialty"]
                if data["work_authorization"] and not (prof.work_authorization or "").strip():
                    prof.work_authorization = data["work_authorization"]
                if data["available_date"] and not prof.available_date:
                    prof.available_date = data["available_date"]
                if data["education"]:
                    prof.education = data["education"]
        db.commit()
    finally:
        db.close()


def _load_done(path: Path) -> set[str]:
    done: set[str] = set()
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["profile_id"])
                except Exception:  # noqa: BLE001
                    continue
    return done


def run(*, budget: float = 10.0, limit: int | None = None, dry_run: bool = False,
        only: str | None = None, manifest: str = "enrich_manifest.jsonl",
        workers: int = 8, flush_every: int = 20) -> dict:
    manifest_path = Path(manifest)
    done = _load_done(manifest_path)
    stats = {k: 0 for k in ("processed", "enriched", "licenses", "compact",
                            "work_rows", "no_text", "llm_error", "empty")}

    db = SessionLocal()
    try:
        q = select(Profile.profile_id, Profile.resume_url).where(
            Profile.resume_url.isnot(None))
        if only:
            q = q.where(Profile.profile_id == only)
        else:
            q = q.order_by(Profile.created_at)
        # Fetch past the already-done rows (tracked in the manifest, not in SQL)
        # before applying the caller's limit, so a small --limit still finds fresh work.
        if limit:
            q = q.limit(limit + len(done) + 100)
        rows = [dict(r._mapping) for r in db.execute(q)]
    finally:
        db.close()
    if not only:
        rows = [r for r in rows if r["profile_id"] not in done][:limit] if limit \
            else [r for r in rows if r["profile_id"] not in done]

    print(f"Enriching up to {len(rows)} profile(s) with {settings.llm_model}, "
          f"budget ${budget:.2f}{' (DRY RUN)' if dry_run else ''} …\n")

    pending: list[dict] = []
    manifest_batch: list[dict] = []

    def flush():
        nonlocal pending, manifest_batch
        if pending and not dry_run:
            _write(pending)
        if manifest_batch and not dry_run:
            with open(manifest_path, "a", encoding="utf-8") as f:
                for rec in manifest_batch:
                    f.write(json.dumps(rec) + "\n")
        pending, manifest_batch = [], []

    stopped = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row, raw in pool.map(_fetch, rows):
            if cost_usd() >= budget:
                stopped = True
                break
            stats["processed"] += 1
            pid = row["profile_id"]
            if raw == "NO_TEXT":
                stats["no_text"] += 1
                outcome = "no_text"
            elif raw is None:
                stats["llm_error"] += 1
                outcome = "llm_error"
            else:
                data = _normalise(raw)
                if data is None:
                    stats["empty"] += 1
                    outcome = "empty"
                else:
                    stats["enriched"] += 1
                    stats["licenses"] += len(data["licenses"])
                    stats["compact"] += sum(1 for l in data["licenses"] if l["is_compact"])
                    stats["work_rows"] += len(data["work_history"])
                    outcome = "enriched"
                    pending.append({"profile_id": pid, "data": data})
            manifest_batch.append({"profile_id": pid, "outcome": outcome})
            if len(manifest_batch) >= flush_every:
                flush()
                print(f"  {stats['processed']:>6} done | {stats['enriched']} enriched"
                      f" | {stats['licenses']} lic ({stats['compact']} compact)"
                      f" | {stats['work_rows']} jobs | ${cost_usd():.2f}")
    flush()

    print("\n--- done ---")
    for k, v in stats.items():
        print(f"  {k:<12} {v}")
    print(f"  {'spend':<12} ${cost_usd():.4f}")
    if stopped:
        print("\nStopped: budget reached. Re-run to continue where it left off.")
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--budget", type=float, default=10.0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", help="single profile_id (re-extracts even if done)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--manifest", default="enrich_manifest.jsonl")
    args = ap.parse_args()
    if not (settings.llm_enabled and settings.llm_base_url
            and settings.llm_model and settings.llm_api_key):
        sys.exit("LLM is not configured (LLM_ENABLED / LLM_MODEL / GEMINI_API_KEY).")
    run(budget=args.budget, limit=args.limit, dry_run=args.dry_run, only=args.only,
        manifest=args.manifest, workers=args.workers)


if __name__ == "__main__":
    main()
