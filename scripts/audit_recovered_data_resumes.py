"""Read-only, resumable screen of recovered Data tab profile headers.

This is a triage tool, not a clinical credential verification. It inspects
every stored profile row and the first 1,400 characters of its extracted source
text. It writes identifiers and reason codes only; no contact or resume text.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.resume_cleanup_common import database_url  # noqa: E402

TECH_TITLE = re.compile(
    r"\b(?:software (?:engineer|developer|architect|manager)|python developer|"
    r"java developer|full stack (?:developer|engineer)|data engineer|"
    r"network (?:engineer|administrator)|systems engineer|"
    r"technical project manager|information technology|IT professional|"
    r"scrum master|business analyst|cybersecurity|web developer)\b", re.I)
CLINICAL_TITLE = re.compile(
    r"\b(?:registered nurse|licensed practical nurse|licensed vocational nurse|"
    r"certified nursing assistant|nurse practitioner|physician assistant|"
    r"physician|medical doctor|pharmacist|pharmacy technician|"
    r"physical therapist|occupational therapist|speech.language pathologist|"
    r"respiratory therapist|radiologic technologist|medical assistant|"
    r"clinical social worker|psychologist|psychiatrist|dentist|"
    r"histotechnologist|surgical technologist)\b", re.I)
NAME_CREDENTIALS = {"MCP", "MCSD", "MCSE", "MCSA", "LPN", "LVN", "CNA", "RN",
                    "FNP", "NP", "APRN", "CRNA", "CNM", "PA-C", "MD", "DO",
                    "DNP", "PHD", "PHARMD", "DPT", "PT", "OT", "DDS", "DMD"}
ROLE_CATEGORY = {
    "licensed practical nurse": "Nursing",
    "licensed vocational nurse": "Nursing",
    "certified nursing assistant": "Nursing",
    "registered nurse": "Nursing",
    "nurse practitioner": "APP",
    "physician assistant": "APP",
    "pharmacist": "Pharmacy",
    "physical therapist": "Therapy",
    "occupational therapist": "Therapy",
}


def reasons(row: tuple) -> list[str]:
    (_, kind, first, last, credential, specialty, category, city, state,
     raw_header, parse_status, review_status) = row
    flags: list[str] = []
    if review_status == "rejected":
        return flags
    if not first or not last:
        flags.append("missing_name")
    elif any(ch.isdigit() for ch in f"{first} {last}") or re.search(r"[;:@|]", f"{first} {last}"):
        flags.append("malformed_name")
    if (last or "").strip().upper() in NAME_CREDENTIALS:
        flags.append("credential_as_surname")
    if not city and not state:
        flags.append("missing_location")
    if not category:
        flags.append("missing_category")
    if not specialty:
        flags.append("missing_specialty")
    if kind not in {"resume", "generated_cv"}:
        flags.append("source_is_not_resume")
    elif parse_status != "parsed" or not raw_header:
        flags.append("resume_text_unavailable")
    else:
        lines = [" ".join(x.split()) for x in raw_header.splitlines() if x.strip()]
        header = " ".join(lines[:12])[:1000]
        if TECH_TITLE.search(header) and not CLINICAL_TITLE.search(header):
            flags.append("nonclinical_technical_header")
        if category:
            for phrase, expected in ROLE_CATEGORY.items():
                pattern = (r"\bregistered nurse\b(?!\s+anesthetist)" if phrase == "registered nurse"
                           else rf"\b{re.escape(phrase)}\b")
                if re.search(pattern, header, re.I) and category != expected:
                    flags.append("header_category_conflict")
                    break
    return flags


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--out", type=Path, default=ROOT / "reports" / "data_tab_resume_audit_20261119.jsonl")
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    url = database_url(args.env_file)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = args.out.with_suffix(".checkpoint.json")
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {"last": "00000000-0000-0000-0000-000000000000", "scanned": 0, "flags": {}}
    counts = Counter(state["flags"])
    started = time.monotonic()
    with psycopg.connect(url, connect_timeout=20) as db, args.out.open("a", encoding="utf-8") as out:
        while True:
            batch_size = min(args.batch_size, args.limit - state["scanned"]) if args.limit else args.batch_size
            if batch_size <= 0:
                break
            with db.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '60000ms'")
                cur.execute('''SELECT p.provider_id::text,p.source_kind,p.first_name,p.last_name,
                                      credential,specialty,job_category,city,state_code,
                                      left(extracted_text,1400),parse_status,review_status
                               FROM public."New_provider" AS p WHERE p.provider_id > %s::uuid
                               ORDER BY p.provider_id LIMIT %s''', (state["last"], batch_size))
                rows = cur.fetchall()
            db.commit()
            if not rows:
                break
            for row in rows:
                flags = reasons(row)
                for flag in flags:
                    counts[flag] += 1
                if flags:
                    out.write(json.dumps({"provider_id": row[0], "flags": flags}, separators=(",", ":")) + "\n")
            out.flush()
            state = {"last": rows[-1][0], "scanned": state["scanned"] + len(rows), "flags": dict(counts)}
            checkpoint.write_text(json.dumps(state), encoding="utf-8")
            print(json.dumps({"scanned": state["scanned"], "seconds": round(time.monotonic()-started, 1), "flags": dict(counts)}), flush=True)
            if len(rows) < batch_size:
                break
    print(json.dumps({"complete": True, **state}), flush=True)


if __name__ == "__main__":
    main()
