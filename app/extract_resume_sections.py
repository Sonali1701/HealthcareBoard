"""Extract clean résumé sections with an LLM into profiles.resume_sections.

The résumé viewer re-parses the source file with regex heuristics every time it
is opened. That fails on the two things real résumés do constantly: PDFs that
lose their inter-word spacing ("Unit:LTC,Skilled,AssistedLiving") and
multi-column layouts whose text extracts in jumbled order. An LLM reads through
both, so we do it once here and store the result.

    python -m app.extract_resume_sections --limit 20        # try a small slice
    python -m app.extract_resume_sections --budget 5        # stop near $5
    python -m app.extract_resume_sections --dry-run --limit 5
    python -m app.extract_resume_sections --only <profile_id>

Budget-capped (uses the same conservative token pricing as the audit) and
resumable: every processed profile is appended to a manifest, so re-running
continues where it stopped and never re-pays for finished work.

Run `python -m app.migrate_resume_sections` first to create the column.
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select, update

from .clean_names_llm import _llm, _resume_text_from_url, cost_usd
from .config import settings
from .database import SessionLocal
from .models import Profile

# Canonical section names the résumé viewer renders, in display order. The LLM
# is asked for exactly these keys so stored output drops straight into the API.
SECTION_KEYS = [
    "Professional Summary",
    "Experience",
    "Education & Training",
    "Certifications & Licensure",
    "Skills",
    "Languages",
    "Professional Memberships",
    "Awards & Honors",
    "Publications & Presentations",
]

SCHEMA_VERSION = 1

_SYSTEM = ("You reorganise a healthcare professional's resume into clean, "
           "structured sections. Respond with ONLY one JSON object and no other text.")

_INSTR = (
    "The RESUME text below was extracted from a PDF or Word file. It may have "
    "lost the spaces between words (\"AssistedLiving\", \"ofsupport\") or have "
    "lines interleaved from a multi-column layout. Reconstruct it faithfully.\n\n"
    "Return JSON exactly like:\n"
    '{"sections":{"Professional Summary":["..."],"Experience":["..."],'
    '"Education & Training":["..."],"Certifications & Licensure":["..."],'
    '"Skills":["..."],"Languages":["..."],"Professional Memberships":["..."],'
    '"Awards & Honors":["..."],"Publications & Presentations":["..."]},'
    '"skills":["..."]}\n\n'
    "Rules:\n"
    "- Use ONLY these section keys. Omit any section the resume does not have — "
    "never invent content, never pad a section to look complete.\n"
    "- Repair lost spacing and reassemble sentences split across columns, but do "
    "NOT reword, summarise or embellish. Keep the candidate's own wording.\n"
    "- Each array item is one readable line: a bullet, a job entry, or a degree. "
    "Put the employer/title/dates of a job on one line, its duties on following "
    "lines.\n"
    "- \"skills\" is a flat list of short skill names (1-4 words each, max 20) for "
    "chips in the UI. No sentences.\n"
    "- EXCLUDE personal contact details: no email addresses, phone numbers or "
    "street addresses anywhere in the output.\n"
    "- Drop page numbers, headers/footers and 'References available on request'.\n"
    "- If the text is unusable (empty, or scanned gibberish), return "
    '{"sections":{},"skills":[]}.'
)


def _clean_lines(value) -> list[str]:
    """Coerce a model-returned section into a list of clean, non-empty lines."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, (dict, list)):
            item = json.dumps(item, ensure_ascii=False)
        line = " ".join(str(item or "").split()).strip(" -•|")
        if line and len(line) <= 600:
            out.append(line)
    return out[:120]


def _normalise(raw: dict | None) -> dict | None:
    """Validate the model's JSON into the stored envelope, or None if unusable."""
    if not isinstance(raw, dict):
        return None
    raw_sections = raw.get("sections")
    if not isinstance(raw_sections, dict):
        return None
    sections: dict[str, list[str]] = {}
    for key in SECTION_KEYS:                      # fixed order, known keys only
        lines = _clean_lines(raw_sections.get(key))
        if lines:
            sections[key] = lines
    skills, seen = [], set()
    for s in _clean_lines(raw.get("skills")):
        if 2 <= len(s) <= 48 and s.lower() not in seen:
            seen.add(s.lower())
            skills.append(s)
    if not sections and not skills:
        return None
    return {"v": SCHEMA_VERSION, "sections": sections, "skills": skills[:20]}


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


def _fetch(row: dict):
    """Network-bound work for one profile (download + LLM). No DB access."""
    text = _resume_text_from_url(row["resume_url"])
    if not text or not text.strip():
        return row, "NO_TEXT"
    return row, _llm(text, system=_SYSTEM, instr=_INSTR, max_chars=12000)


def _write(pending: list[dict]) -> None:
    """Persist a batch through a short-lived session, so the DB connection is
    only open for the fast write — never during the slow LLM calls (Neon drops
    idle connections)."""
    if not pending:
        return
    db = SessionLocal()
    try:
        for u in pending:
            db.execute(update(Profile)
                       .where(Profile.profile_id == u["profile_id"])
                       .values(resume_sections=u["resume_sections"]))
        db.commit()
    finally:
        db.close()


def run(*, budget: float = 10.0, limit: int | None = None, dry_run: bool = False,
        only: str | None = None, manifest: str = "resume_sections_manifest.jsonl",
        workers: int = 8, flush_every: int = 25) -> dict:
    manifest_path = Path(manifest)
    done = _load_done(manifest_path)
    stats = {k: 0 for k in ("processed", "extracted", "no_text", "llm_error", "empty")}

    db = SessionLocal()
    try:
        q = select(Profile.profile_id, Profile.resume_url).where(
            Profile.resume_url.isnot(None))
        if only:
            q = q.where(Profile.profile_id == only)
        else:
            # Un-extracted first; oldest first so runs are deterministic.
            q = q.where(Profile.resume_sections.is_(None)).order_by(Profile.created_at)
        if limit:
            q = q.limit(limit)
        rows = [dict(r._mapping) for r in db.execute(q)]
    finally:
        db.close()
    if not only:
        rows = [r for r in rows if r["profile_id"] not in done]

    print(f"Extracting sections for up to {len(rows)} résumé(s) with "
          f"{settings.llm_model}, budget ${budget:.2f}"
          f"{' (DRY RUN)' if dry_run else ''} …\n")

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
                    stats["extracted"] += 1
                    outcome = "extracted"
                    pending.append({"profile_id": pid, "resume_sections": data})

            manifest_batch.append({"profile_id": pid, "outcome": outcome})
            if len(manifest_batch) >= flush_every:
                flush()
                print(f"  {stats['processed']:>6} processed | "
                      f"{stats['extracted']} extracted | "
                      f"{stats['no_text']} no-text | ${cost_usd():.2f}")
    flush()

    print("\n--- done ---")
    for k, v in stats.items():
        print(f"  {k:<12} {v}")
    print(f"  {'spend':<12} ${cost_usd():.2f}")
    if stopped:
        print("\nStopped: budget reached. Re-run to continue where it left off.")
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--budget", type=float, default=10.0, help="stop near this USD spend")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", help="single profile_id (re-extracts even if done)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true", help="read + LLM, write nothing")
    ap.add_argument("--manifest", default="resume_sections_manifest.jsonl")
    args = ap.parse_args()

    if not (settings.llm_enabled and settings.llm_base_url
            and settings.llm_model and settings.llm_api_key):
        sys.exit("LLM is not configured. Set LLM_ENABLED / LLM_BASE_URL / "
                 "LLM_MODEL / LLM_API_KEY (or GEMINI_API_KEY) in .env")

    run(budget=args.budget, limit=args.limit, dry_run=args.dry_run, only=args.only,
        manifest=args.manifest, workers=args.workers)


if __name__ == "__main__":
    main()
