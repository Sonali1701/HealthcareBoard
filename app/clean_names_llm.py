"""Re-clean existing profile names with an LLM (OpenAI / ChatGPT).

For each profile that has a résumé, the model re-reads the file and returns the
real person's name (+ specialty / city / state / zip). Validated names replace
the old junk and the profile is un-hidden (is_listable=True); profiles the model
can't find a real name for stay hidden.

    python -m app.clean_names_llm --limit 20      # test on 20 hidden profiles
    python -m app.clean_names_llm                  # all currently-hidden profiles
    python -m app.clean_names_llm --all --limit 100  # any profile with a résumé

Reads LLM_* from .env. Resumable: salvaged profiles flip to listable, so
re-running the default (hidden-only) never reprocesses them. After a big run,
re-run `python -m app.migrate_geocode` to geocode the newly-found ZIPs.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time

import httpx
from sqlalchemy import select, update

from .config import settings
from .database import SessionLocal
from .importers.parsing import (classify_provider, extract_text_from_bytes,
                                is_real_name)
from .models import Profile
from .services import storage

_SYSTEM = ("You extract structured data from a healthcare professional's resume. "
           "Respond with ONLY one JSON object and no other text.")
_INSTR = (
    "Extract these fields. Use null when a field is absent — never guess.\n"
    'Return JSON exactly like:\n'
    '{"first_name":null,"last_name":null,"profession":null,"specialty":null,'
    '"city":null,"state":null,"zip":null,"years_experience":null}\n'
    "- first_name/last_name = the ACTUAL person's name. If the top of the resume "
    "is a document title ('Resume', 'Curriculum Vitae'), a role/title "
    "('Registered Nurse', 'Professional Profile'), or a section header, set BOTH "
    "names to null.\n"
    "- profession = license/credential: RN, LPN, CNA, NP, CRNA, PA, MD, DO, RT, PT, OT...\n"
    "- state = 2-letter US code. zip = 5 digits. years_experience = a whole number."
)


def _title(value) -> str | None:
    return " ".join(w[:1].upper() + w[1:] for w in str(value or "").split()) or None


def _extract_json_object(content: str) -> str | None:
    """Pull the first complete JSON object from a model reply.

    Robust to markdown ```json fences and to 'thinking' models (e.g. Gemini
    flash) that leak reasoning prose around the JSON. Walks brace depth while
    respecting string literals, so it stops at the first balanced {...}.
    """
    if not content:
        return None
    content = content.strip()
    if content.startswith("```"):                 # strip a ```json … ``` fence
        content = content.lstrip("`")
        if content[:4].lower() == "json":
            content = content[4:]
        content = content.split("```", 1)[0]
    start = content.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(content)):
        c = content[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return content[start:i + 1]
    return None


def _retry_after_seconds(r) -> float | None:
    """How long to wait after a 429, from the Retry-After header or the body's
    'Please retry in 14.7s' hint (Gemini). None if not specified."""
    ra = r.headers.get("Retry-After")
    if ra:
        try:
            return float(ra)
        except ValueError:
            pass
    m = re.search(r"retry in ([\d.]+)s", r.text)
    return float(m.group(1)) if m else None


# Token accounting so bulk jobs can enforce a $ budget. Prices are a conservative
# upper bound for Gemini flash-lite (USD per 1M tokens); actual is ≤ this, so a
# budget check using these never lets real spend exceed the cap. The lock keeps
# the counter correct when many LLM calls run concurrently.
_PRICE_IN = 0.10
_PRICE_OUT = 0.40
_USAGE = {"in": 0, "out": 0, "calls": 0}
_USAGE_LOCK = threading.Lock()


def cost_usd() -> float:
    return _USAGE["in"] / 1e6 * _PRICE_IN + _USAGE["out"] / 1e6 * _PRICE_OUT


def _llm(text: str) -> dict | None:
    body = {
        "model": settings.llm_model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _INSTR + "\n\nRESUME:\n" + text[:6000]},
        ],
        "response_format": {"type": "json_object"},
    }
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": "Bearer " + settings.llm_api_key}
    for attempt in range(6):
        try:
            r = httpx.post(url, json=body, timeout=settings.llm_timeout, headers=headers)
            if r.status_code == 429:            # rate limited — honor the hint
                delay = _retry_after_seconds(r) or (2 * (attempt + 1))
                time.sleep(min(delay + 0.5, 65))
                continue
            r.raise_for_status()
            payload = r.json()
            usage = payload.get("usage") or {}
            with _USAGE_LOCK:
                _USAGE["in"] += usage.get("prompt_tokens", 0)
                _USAGE["out"] += usage.get("completion_tokens", 0)
                _USAGE["calls"] += 1
            content = payload["choices"][0]["message"]["content"]
            blob = _extract_json_object(content)
            return json.loads(blob) if blob else None
        except Exception:  # noqa: BLE001
            if attempt == 5:
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def _resume_text_from_url(url: str | None) -> str | None:
    try:
        key, is_local = storage.key_from_url(url)
        data = storage.download_bytes(key, prefer_local=is_local)
        return extract_text_from_bytes(data, key)
    except Exception:  # noqa: BLE001
        return None


# Columns loaded up front. The LLM overwrites name/specialty/city/state/zip; the
# rest (headline/bio/profession/board) are only needed to rebuild search_text.
_LOAD_COLS = (
    Profile.profile_id, Profile.resume_url, Profile.first_name, Profile.last_name,
    Profile.headline, Profile.bio, Profile.specialty, Profile.profession_type,
    Profile.american_board, Profile.provider_category, Profile.city, Profile.state_code,
)


def _apply(pending: list[dict]) -> None:
    """Write a batch of salvaged updates through a short-lived session, so the DB
    connection is only ever open for the fast write — never during the slow LLM
    calls (Neon drops idle connections, which is what crashed the naive version)."""
    if not pending:
        return
    db = SessionLocal()
    try:
        for u in pending:
            vals = {k: v for k, v in u.items() if k != "profile_id"}
            db.execute(update(Profile).where(
                Profile.profile_id == u["profile_id"]).values(**vals))
        db.commit()
    finally:
        db.close()


def run(junk_only: bool = True, limit: int | None = None, flush_every: int = 10) -> dict:
    stats = {"processed": 0, "salvaged": 0, "no_name": 0, "no_text": 0, "failed": 0}

    # Phase 1 — load the work list, then release the DB connection entirely.
    db = SessionLocal()
    try:
        q = select(*_LOAD_COLS).where(Profile.resume_url.isnot(None))
        if junk_only:
            q = q.where(Profile.is_listable.is_(False))
        q = q.order_by(Profile.created_at)
        if limit:
            q = q.limit(limit)
        rows = [dict(r._mapping) for r in db.execute(q)]
    finally:
        db.close()

    total = len(rows)
    print(f"Cleaning {total} profile(s) with {settings.llm_model} …\n")

    # Phase 2 — LLM per profile with NO DB connection held; flush in small bursts.
    pace = max(settings.llm_min_interval, 0.0)
    pending: list[dict] = []
    for i, row in enumerate(rows, 1):
        stats["processed"] += 1
        text = _resume_text_from_url(row["resume_url"])
        if not text:
            stats["no_text"] += 1
            continue
        if pace and i > 1:
            time.sleep(pace)
        raw = _llm(text)
        if raw is None:
            stats["failed"] += 1
            continue
        first, last = _title(raw.get("first_name")), _title(raw.get("last_name"))
        if not (first and last and is_real_name(first, last)):
            stats["no_name"] += 1
            continue

        specialty = (_title(raw["specialty"]) if raw.get("specialty") else None) or row["specialty"]
        city = (_title(raw["city"]) if raw.get("city") else None) or row["city"]
        st = str(raw.get("state") or "").strip().upper()
        state_code = st if len(st) == 2 else row["state_code"]
        category = classify_provider(
            row["profession_type"], specialty, row["headline"]) or row["provider_category"]

        tmp = Profile(
            first_name=first, last_name=last, headline=row["headline"], bio=row["bio"],
            specialty=specialty, profession_type=row["profession_type"], city=city,
            state_code=state_code, american_board=row["american_board"],
            provider_category=category)
        tmp.rebuild_search_text()

        upd = {
            "profile_id": row["profile_id"],
            "first_name": first[:100],
            "last_name": last[:100],
            "specialty": specialty[:100] if specialty else None,
            "city": city[:120] if city else None,
            "state_code": state_code,
            "provider_category": category,
            "is_listable": True,
            "search_text": tmp.search_text,
        }
        z = str(raw.get("zip") or "").strip()
        if re.fullmatch(r"\d{5}", z):
            upd["zip_code"] = z
        pending.append(upd)
        stats["salvaged"] += 1
        print(f"  [{i}/{total}] {row['first_name']} {row['last_name']!r} -> {first} {last}")

        if len(pending) >= flush_every:
            _apply(pending)
            pending = []
            print(f"  … {i}/{total} processed, {stats['salvaged']} salvaged (committed)")

    _apply(pending)
    print(f"\nDone: {stats}")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-clean profile names with an LLM")
    ap.add_argument("--all", action="store_true",
                    help="process every profile with a résumé (not just hidden ones)")
    ap.add_argument("--limit", type=int, default=None, help="only process the first N")
    args = ap.parse_args()
    if not (settings.llm_enabled and settings.llm_base_url
            and settings.llm_model and settings.llm_api_key):
        print("ERROR: set LLM_ENABLED / LLM_BASE_URL / LLM_API_KEY / LLM_MODEL in .env")
        return 1
    run(junk_only=not args.all, limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
