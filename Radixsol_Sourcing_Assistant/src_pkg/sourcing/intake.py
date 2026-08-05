"""
Candidate intake — parse a list of candidate names + locations from CSV or pasted
text. These are names you are entitled to work with (your ATS export, applicants,
referrals, a manual list). The tool never scrapes a platform for names.
"""
from __future__ import annotations

import csv
import io
import re


def parse_csv(text: str) -> list[dict]:
    """CSV with headers containing 'name' and optionally 'location'/'city'/'state'."""
    rows = []
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return rows
    fmap = {f.lower().strip(): f for f in reader.fieldnames}
    name_key = next((fmap[k] for k in ("name", "candidate", "full name", "fullname") if k in fmap), None)
    loc_key = next((fmap[k] for k in ("location", "city", "citystate", "city/state", "area") if k in fmap), None)
    state_key = fmap.get("state")
    for r in reader:
        if not name_key:
            break
        name = (r.get(name_key) or "").strip()
        if not name:
            continue
        loc = (r.get(loc_key) or "").strip() if loc_key else ""
        if state_key and r.get(state_key):
            loc = f"{loc}, {r[state_key].strip()}".strip(", ")
        rows.append({"name": name, "location": loc})
    return rows


def parse_pasted(text: str) -> list[dict]:
    """One candidate per line. Accepts:
       'Jane Doe'  |  'Jane Doe, Atlanta, GA'  |  'Jane Doe - Atlanta GA'  |  'Jane Doe | Atlanta, GA'
    """
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"\s*[|\-\u2013\u2014]\s*|,\s*", line, maxsplit=1)
        name = parts[0].strip()
        loc = parts[1].strip() if len(parts) > 1 else ""
        if name:
            out.append({"name": name, "location": loc})
    return out


def parse(text: str) -> list[dict]:
    """Auto-detect CSV (has header row with 'name') vs pasted lines."""
    head = text.splitlines()[0].lower() if text.strip() else ""
    if "," in head and "name" in head:
        rows = parse_csv(text)
        if rows:
            return rows
    return parse_pasted(text)
