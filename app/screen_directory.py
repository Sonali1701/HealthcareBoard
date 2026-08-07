"""Screen the provider directory for résumés that are not healthcare at all.

The bulk import brought in a large population of IT/admin résumés and some
empty files. The existing ingest check only asked whether the *name* looked
real, so these sit in the directory as "Others" with no profession, polluting
search results, match runs and pools.

This reads each suspect résumé from storage and scores it on healthcare signal.
Clinical vocabulary is distinctive enough that keyword evidence settles most
cases for free, deterministically and explainably. `--use-llm` adds a model
call for the narrow uncertain band only (0-1 clinical terms), where keywords
genuinely cannot tell a nurse from a hospital front-desk clerk.

Design rules:
  * Conservative — a profile is hidden only when healthcare evidence is
    essentially ABSENT, not merely when IT evidence is present. A nurse who
    lists Epic and SQL stays.
  * Auditable — every decision writes screen_reason/screen_score/screened_at,
    so `--restore` can undo the whole sweep.
  * Resumable — each processed id is appended to a manifest, so a re-run picks
    up where it stopped.

Run:
    python -m app.screen_directory --dry-run --limit 300
    python -m app.screen_directory --scope clinical --workers 6
    python -m app.screen_directory --scope clinical --use-llm   # arbitrate the grey area
    python -m app.screen_directory --restore        # undo every hide this made
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import func, or_, select, text as sqltext

from .database import SessionLocal, utcnow
from .models import Profile

MANIFEST = "screen_manifest.jsonl"
SCREEN_REASON_NOT_HEALTHCARE = "not_healthcare"
SCREEN_REASON_EMPTY = "empty_resume"
SCREEN_REASON_KEPT = "healthcare_ok"
SCREEN_REASON_LLM_KEPT = "healthcare_llm"       # keyword screen said no, model said yes
SCREEN_REASON_LLM_REJECTED = "not_healthcare_llm"  # both agreed it is not clinical

# Distinctive clinical vocabulary. Deliberately excludes ambiguous words like
# "care", "support" or "assistant" that appear in plenty of IT résumés.
_HEALTHCARE = re.compile(
    r"\b("
    r"registered nurse|licensed practical nurse|nurse practitioner|nursing assistant|"
    r"\bRN\b|\bLPN\b|\bLVN\b|\bCNA\b|\bCRNA\b|\bAPRN\b|\bPACU\b|\bICU\b|\bNICU\b|\bPICU\b|"
    r"\bBSN\b|\bMSN\b|\bNCLEX\b|\bBLS\b|\bACLS\b|\bPALS\b|\bCPR certified\b|"
    r"patient care|patient safety|patients|clinical|bedside|charge nurse|staff nurse|"
    r"med[- ]?surg|telemetry|phlebotomy|venipuncture|catheter|foley|"
    r"wound care|vital signs|medication administration|IV therapy|infusion|"
    r"triage|emergency department|operating room|perioperative|labor and delivery|"
    r"hospice|home health|long[- ]term care|skilled nursing|rehabilitation facility|"
    r"physical therapist|occupational therapist|speech language patholog|"
    r"respiratory therap|radiolog|sonograph|phlebotomist|paramedic|\bEMT\b|"
    r"physician|surgeon|anesthesiolog|cardiolog|oncolog|pediatric|geriatric|"
    r"epic systems|cerner|meditech|\bEMR\b|\bEHR\b|\bHIPAA\b|"
    r"nursing home|medical surgical|acute care|ambulatory|dialysis|"
    r"board of nursing|state license|nursing license"
    r")\b", re.I)

# Only used to explain a decision, never to force one on its own.
_TECH = re.compile(
    r"\b(java|javascript|typescript|python|\.net|c\+\+|c#|sql server|oracle|"
    r"kubernetes|docker|aws|azure|devops|scrum master|agile|jira|selenium|"
    r"software (engineer|developer|test)|quality assurance analyst|qa engineer|"
    r"web developer|full stack|front[- ]end|back[- ]end|weblogic|websphere|"
    r"data engineer|business analyst|salesforce|sap|etl|tableau|power bi)\b", re.I)

_lock = threading.Lock()


def _load_done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done = set()
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except Exception:
                continue
            # Errors are transient (dropped connection, unreadable byte range):
            # leave them out so a re-run gets another shot at them.
            if row.get("reason") != "error":
                done.add(row["profile_id"])
    return done


def _append(path: Path, row: dict) -> None:
    with _lock, path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def adjudicate(txt: str) -> bool | None:
    """Ask the LLM whether a résumé is a clinical healthcare worker.

    Only used for the narrow band the keyword screen cannot settle. Keywords
    handle the clear cases for free and deterministically; spending a model
    call on "Registered Nurse, ICU, ACLS" would be waste. Returns None when the
    model is unavailable or unclear, so the caller keeps its own verdict.
    """
    from .clean_names_llm import _llm

    out = _llm(
        (txt or "")[:4000],
        system="You classify résumés for a healthcare staffing directory. Reply ONLY with JSON.",
        instr=('Return {"healthcare": true|false, "role": "<short role title>"}. '
               '"healthcare" is true ONLY for people who deliver clinical care to '
               'patients (nurses, physicians, therapists, technologists, aides, '
               'paramedics). It is false for IT, finance, admin, retail, '
               'engineering and other non-clinical work, even at a hospital.'),
        retries=2, timeout=30,
    )
    if not isinstance(out, dict) or "healthcare" not in out:
        return None
    return bool(out["healthcare"])


def classify(txt: str, strict: bool = False) -> tuple[bool, str, int]:
    """-> (keep, reason, healthcare_hit_count).

    `strict=True` is used when re-screening profiles the importer ALREADY put in
    a clinical category. Something in those résumés once read as healthcare, so
    the bar to overturn that is higher: hide only when there is no clinical
    vocabulary whatsoever.
    """
    if not txt or len(txt.strip()) < 40:
        return False, SCREEN_REASON_EMPTY, 0
    hits = len(set(m.group(0).lower() for m in _HEALTHCARE.finditer(txt)))
    if hits >= 2:
        return True, SCREEN_REASON_KEPT, hits
    if strict:
        # A genuine clinical résumé always says *something* — "patient", "RN",
        # "clinical". Only a total absence justifies overriding the category.
        return (hits >= 1), (SCREEN_REASON_KEPT if hits else SCREEN_REASON_NOT_HEALTHCARE), hits
    if hits == 1 and not _TECH.search(txt):
        # One clinical term and nothing contradicting it: keep, and let a human
        # judge. False negatives cost a real candidate; false positives cost noise.
        return True, SCREEN_REASON_KEPT, hits
    return False, SCREEN_REASON_NOT_HEALTHCARE, hits


_CLINICAL_CATEGORIES = ("Nursing", "Physicians", "Allied", "APP")


def _suspects(db, limit: int | None, done: set[str],
              scope: str = "others") -> list[tuple[str, str]]:
    """Profiles worth re-reading.

    scope="others"     — no role signal at all (category Others/NULL, no
                         profession, no specialty). Where the junk sample came from.
    scope="clinical"   — sits in a clinical category but carries weak evidence
                         (no specialty AND no parsed skills). ~12% of these turn
                         out to be IT/admin résumés miscategorised at import,
                         and they do the most damage because recruiters filter
                         straight to these categories.
    scope="all"        — every unscreened profile, including those the two
                         scopes above skip because the parser found a specialty
                         or skills.

                         That exclusion turned out not to hold. Screening a
                         random 200 of the "has a specialty" population
                         rejected 11%: a Salesforce consultant and an SAP ABAP
                         consultant both carried specialty "ICU", and one row
                         was a data-governance policy document rather than a
                         résumé at all. A parsed specialty is a guess by the
                         importer, not evidence, so it cannot exempt a row from
                         being read.
    """
    base = [Profile.is_listable.is_(True),
            Profile.resume_url.isnot(None),
            Profile.screen_reason.is_(None)]
    if scope == "all":
        conds = base
    elif scope == "clinical":
        from .models import ProfileSkill
        conds = base + [
            Profile.provider_category.in_(_CLINICAL_CATEGORIES),
            or_(Profile.specialty.is_(None), Profile.specialty == ""),
            ~select(ProfileSkill.profile_id)
            .where(ProfileSkill.profile_id == Profile.profile_id).exists(),
        ]
    else:
        conds = base + [
            or_(Profile.provider_category == "Others",
                Profile.provider_category.is_(None)),
            or_(Profile.profession_type.is_(None), Profile.profession_type == ""),
            or_(Profile.specialty.is_(None), Profile.specialty == ""),
        ]
    stmt = (select(Profile.profile_id, Profile.resume_url)
            .where(*conds).order_by(Profile.profile_id))
    if limit:
        stmt = stmt.limit(limit + len(done) + 200)
    rows = [(p, u) for p, u in db.execute(stmt).all() if p not in done]
    return rows[:limit] if limit else rows


def run(limit: int | None = None, workers: int = 6, dry_run: bool = False,
        manifest: str = MANIFEST, scope: str = "others",
        use_llm: bool = False) -> None:
    from .services import storage
    from .importers.parsing import extract_text_from_bytes

    strict = scope == "clinical"
    path = Path(manifest)
    done = _load_done(path)
    db = SessionLocal()
    try:
        targets = _suspects(db, limit, done, scope)
    finally:
        db.close()
    print(f"{len(targets):,} profiles to screen "
          f"({len(done):,} already in {manifest})")
    if not targets:
        return

    stats = {"kept": 0, "not_healthcare": 0, "empty_resume": 0,
             SCREEN_REASON_LLM_REJECTED: 0, "error": 0}

    def _persist(pid: str, reason: str, hits: int, keep: bool) -> bool:
        """Write one verdict, retrying transient drops.

        Neon closes pooled connections aggressively under concurrency, and a
        dropped socket must not lose a whole multi-hour sweep.
        """
        for attempt in range(4):
            s = SessionLocal()
            try:
                p = s.get(Profile, pid)
                if p:
                    p.screen_reason = reason
                    p.screen_score = hits
                    p.screened_at = utcnow()
                    if not keep:
                        p.is_listable = False
                    s.commit()
                return True
            except Exception:
                s.rollback()
                if attempt == 3:
                    return False
                time.sleep(1.5 * (attempt + 1))
            finally:
                s.close()
        return False

    def work(item: tuple[str, str]) -> None:
        pid, url = item
        try:
            key, _ = storage.key_from_url(url)
            txt = extract_text_from_bytes(storage.download_bytes(key), key) or ""
            keep, reason, hits = classify(txt, strict=strict)
            # Only the uncertain band goes to the model — 0 or 1 clinical terms,
            # with enough text to judge. It arbitrates in BOTH directions: the
            # costly error is a hospital front-desk clerk kept on the word
            # "patients", not just a nurse wrongly hidden. Anything with real
            # clinical vocabulary, or no text at all, is settled for free.
            if use_llm and hits <= 1 and reason != SCREEN_REASON_EMPTY \
                    and len(txt.strip()) >= 200:
                verdict = adjudicate(txt)
                if verdict is True:
                    keep, reason = True, SCREEN_REASON_LLM_KEPT
                elif verdict is False:
                    keep, reason = False, SCREEN_REASON_LLM_REJECTED
        except Exception as exc:                       # unreadable file: leave it alone
            with _lock:
                stats["error"] += 1
            if not dry_run:
                _append(path, {"profile_id": pid, "reason": "error", "error": str(exc)[:120]})
            return

        # Persist BEFORE logging: the manifest must only ever record work that
        # actually landed, or a resume would skip a profile that was never written.
        if not dry_run and not _persist(pid, reason, hits, keep):
            with _lock:
                stats["error"] += 1
            _append(path, {"profile_id": pid, "reason": "error", "error": "db write failed"})
            return

        with _lock:
            key = "kept" if keep else reason
            stats[key] = stats.get(key, 0) + 1
        # A dry run must not touch the manifest. It writes no screen_reason, so
        # the manifest is the only thing that would stop a later real run from
        # picking the profile up — recording it here silently skipped 300
        # profiles that had never actually been screened.
        if not dry_run:
            _append(path, {"profile_id": pid, "reason": reason, "hits": hits, "keep": keep})

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, _ in enumerate(pool.map(work, targets), 1):
            if i % 250 == 0:
                print(f"  {i:,}/{len(targets):,}  {stats}", flush=True)

    print(f"\n{'DRY RUN - nothing written' if dry_run else 'done'}: {stats}")
    if not dry_run:
        db = SessionLocal()
        try:
            listable = db.scalar(select(func.count()).select_from(Profile)
                                 .where(Profile.is_listable.is_(True)))
            print(f"directory now lists {listable:,} profiles")
        finally:
            db.close()


RESCUE_MANIFEST = "screen_rescue_manifest.jsonl"


def rescue(limit: int | None = None, workers: int = 6, dry_run: bool = False,
           manifest: str = RESCUE_MANIFEST) -> None:
    """Re-read the profiles the keyword pass rejected, and ask the model.

    Running the LLM over every row costs about six times what the keyword pass
    costs and mostly re-confirms decisions keywords already made confidently.
    The rejections are where it earns its keep: on the earlier clinical sweep
    the model overturned roughly one keyword rejection in five. This targets
    only ``not_healthcare``, so the expensive judgement is spent where a wrong
    answer removes a real candidate from the directory.

    A rescued profile is relisted and marked ``healthcare_llm``, so the change
    stays as auditable and reversible as the original hide.
    """
    from .services import storage
    from .importers.parsing import extract_text_from_bytes

    path = Path(manifest)
    done = _load_done(path)
    db = SessionLocal()
    try:
        stmt = (select(Profile.profile_id, Profile.resume_url)
                .where(Profile.screen_reason == SCREEN_REASON_NOT_HEALTHCARE,
                       Profile.resume_url.isnot(None))
                .order_by(Profile.profile_id))
        if limit:
            stmt = stmt.limit(limit + len(done) + 200)
        targets = [(p, u) for p, u in db.execute(stmt).all() if p not in done]
    finally:
        db.close()
    if limit:
        targets = targets[:limit]
    print(f"{len(targets):,} rejected profiles to re-check "
          f"({len(done):,} already in {manifest})")
    if not targets:
        return

    stats = {"rescued": 0, "upheld": 0, "unclear": 0, "error": 0}

    def _relist(pid: str) -> bool:
        for attempt in range(4):
            s = SessionLocal()
            try:
                p = s.get(Profile, pid)
                if p:
                    p.is_listable = True
                    p.screen_reason = SCREEN_REASON_LLM_KEPT
                    p.screened_at = utcnow()
                    s.commit()
                return True
            except Exception:
                s.rollback()
                if attempt == 3:
                    return False
                time.sleep(1.5 * (attempt + 1))
            finally:
                s.close()
        return False

    def work(item: tuple[str, str]) -> None:
        pid, url = item
        try:
            key, _ = storage.key_from_url(url)
            txt = extract_text_from_bytes(storage.download_bytes(key), key) or ""
        except Exception as exc:                       # noqa: BLE001
            with _lock:
                stats["error"] += 1
            if not dry_run:
                _append(path, {"profile_id": pid, "reason": "error",
                               "error": str(exc)[:120]})
            return
        if len(txt.strip()) < 200:
            with _lock:
                stats["upheld"] += 1
            if not dry_run:
                _append(path, {"profile_id": pid, "reason": "upheld_too_short"})
            return

        verdict = adjudicate(txt)
        if verdict is None:
            with _lock:
                stats["unclear"] += 1
            # No answer is not a verdict: leave the hide in place, but keep the
            # row retryable rather than recording it as settled.
            if not dry_run:
                _append(path, {"profile_id": pid, "reason": "error",
                               "error": "model unavailable"})
            return
        if verdict is False:
            with _lock:
                stats["upheld"] += 1
            if not dry_run:
                _append(path, {"profile_id": pid, "reason": "upheld"})
            return

        if not dry_run and not _relist(pid):
            with _lock:
                stats["error"] += 1
            _append(path, {"profile_id": pid, "reason": "error",
                           "error": "db write failed"})
            return
        with _lock:
            stats["rescued"] += 1
        if not dry_run:
            _append(path, {"profile_id": pid, "reason": "rescued"})

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, _ in enumerate(pool.map(work, targets), 1):
            if i % 250 == 0:
                print(f"  {i:,}/{len(targets):,}  {stats}", flush=True)

    print(f"\n{'DRY RUN - nothing written' if dry_run else 'done'}: {stats}")
    if not dry_run:
        db = SessionLocal()
        try:
            listable = db.scalar(select(func.count()).select_from(Profile)
                                 .where(Profile.is_listable.is_(True)))
            print(f"directory now lists {listable:,} profiles")
        finally:
            db.close()


def restore(manifest: str = MANIFEST) -> None:
    """Undo every hide this screen made (screen_reason is the marker)."""
    db = SessionLocal()
    try:
        n = db.execute(sqltext(
            "UPDATE profiles SET is_listable = TRUE "
            "WHERE screen_reason IN (:a, :b)"),
            {"a": SCREEN_REASON_NOT_HEALTHCARE, "b": SCREEN_REASON_EMPTY}).rowcount
        db.execute(sqltext(
            "UPDATE profiles SET screen_reason = NULL, screen_score = NULL, "
            "screened_at = NULL WHERE screen_reason IS NOT NULL"))
        db.commit()
        print(f"restored {n:,} profiles to the directory; screening cleared")
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--use-llm", action="store_true",
                    help="Let the model adjudicate résumés the keyword screen "
                         "cannot settle (needs an LLM key configured)")
    ap.add_argument("--scope", choices=("others", "clinical", "all"), default="others",
                    help="'others' = no role signal; 'clinical' = weak-evidence "
                         "profiles already sitting in a clinical category; "
                         "'all' = every unscreened profile, including ones a "
                         "parsed specialty would otherwise exempt")
    ap.add_argument("--rescue", action="store_true",
                    help="Re-check only the profiles the keyword pass rejected, "
                         "asking the model, and relist the ones it overturns")
    a = ap.parse_args()
    if a.restore:
        restore(a.manifest)
    elif a.rescue:
        rescue(limit=a.limit, workers=a.workers, dry_run=a.dry_run,
               manifest=(a.manifest if a.manifest != MANIFEST else RESCUE_MANIFEST))
    else:
        run(limit=a.limit, workers=a.workers, dry_run=a.dry_run,
            manifest=a.manifest, scope=a.scope, use_llm=a.use_llm)
