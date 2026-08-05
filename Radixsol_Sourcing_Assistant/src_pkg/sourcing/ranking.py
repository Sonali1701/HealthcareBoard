"""
Fit ranking: score each candidate 0-100 against a job description using keyword
overlap (deterministic, offline). Optional Gemini re-rank can layer on later; the
lexical score is the always-available floor. Updates fit_score in the store.
"""
from __future__ import annotations

import re

from . import store

_TOKEN = re.compile(r"[a-z0-9][a-z0-9+/#.\-]+")
_STOP = set("the a an and or of to in for on with by is are be as at from this that will "
            "we our you your they their it its must shall required experience years".split())


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall((text or "").lower()) if t not in _STOP and len(t) > 2}


def score_candidate(candidate: dict, job: dict) -> float:
    """0-100. Uses candidate name+location+notes vs job title+description.
    (Enformion gives contact data, not skills, so ranking leans on any notes/resume
    text you attach plus title/location proximity — extend as you add resume text.)"""
    job_tokens = _tokens(f"{job.get('title','')} {job.get('description','')}")
    if not job_tokens:
        return 0.0
    cand_text = f"{candidate.get('name','')} {candidate.get('location','')} {candidate.get('notes','')}"
    cand_tokens = _tokens(cand_text)
    overlap = len(job_tokens & cand_tokens)
    base = min(1.0, overlap / max(4, len(job_tokens) * 0.5))

    # boosts: location match + successfully enriched (contactable) candidates rank higher
    loc = (candidate.get("location") or "").lower()
    jloc = (job.get("location") or "").lower()
    loc_boost = 0.15 if jloc and any(part in loc for part in jloc.replace(",", " ").split() if len(part) > 2) else 0
    contact_boost = 0.15 if candidate.get("enrich_status") == "success" else 0
    return round(min(100.0, (base * 0.7 + loc_boost + contact_boost) * 100), 1)


def rank_job(job_id: int) -> dict:
    job = store.get_job(job_id)
    if not job:
        return {"error": "job not found"}
    cands = store.list_candidates(job_id=job_id)
    for c in cands:
        s = score_candidate(c, job)
        store.update_candidate(c["id"], fit_score=s)
    return {"ranked": len(cands), "job": job["title"]}
