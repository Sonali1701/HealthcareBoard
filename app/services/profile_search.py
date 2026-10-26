"""Search provider identity and taxonomy fields without stale search_text."""
from __future__ import annotations

import re
import gzip
import json
from collections import Counter
from functools import lru_cache
from pathlib import Path

from sqlalchemy import and_, func, literal, literal_column, or_

from ..models import Profile


_EMPTY = literal_column("''")
_SPACE = literal_column("' '")
# Keep this expression in sync with ix_profiles_directory_search_trgm. It
# contains only the four requested fields and needs no data backfill.
_SEARCH_VALUE = func.lower(
    func.coalesce(Profile.first_name, _EMPTY) + _SPACE
    + func.coalesce(Profile.last_name, _EMPTY) + _SPACE
    + func.coalesce(Profile.specialty, _EMPTY) + _SPACE
    + func.coalesce(Profile.job_category, _EMPTY)
)
_NAME_VALUE = func.lower(
    func.coalesce(Profile.first_name, _EMPTY) + _SPACE
    + func.coalesce(Profile.last_name, _EMPTY)
)
_ROLE_VALUES = (
    func.lower(func.coalesce(Profile.specialty, _EMPTY)),
    func.lower(func.coalesce(Profile.job_category, _EMPTY)),
)
_ROLE_WORDS = {
    "nurse", "nurses", "nursing", "practitioner", "physician", "physicians",
    "doctor", "surgeon", "assistant", "therapist", "pharmacist", "dentist",
    "hygienist", "psychologist", "psychiatrist", "counselor", "counsellor",
    "technician", "technologist", "paramedic", "chiropractor", "podiatrist",
    "optometrist", "audiologist", "midwife", "anesthetist", "radiologist",
    "pathologist", "aide",
}


@lru_cache(maxsize=1)
def _title_categories() -> dict:
    # Versioned lookup derived from the same CSV used to populate job_category.
    path = Path(__file__).resolve().parents[1] / "data" / "provider_title_categories.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as source:
        return json.load(source)


@lru_cache(maxsize=512)
def _role_category(query: str) -> str | None:
    words = re.findall(r"\w+", query.casefold())
    if not words or not _ROLE_WORDS.intersection(words):
        return None
    titles = _title_categories()
    normalized = " ".join(words)
    if normalized in titles:
        return titles[normalized][0]
    # For phrases absent as exact titles, infer only when the matching titles
    # overwhelmingly share one category. Ambiguous roles remain unrestricted.
    weights = Counter()
    for title, (category, count) in titles.items():
        padded = f" {title} "
        if all(f" {word} " in padded for word in words):
            weights[category] += count
    if weights:
        category, weight = weights.most_common(1)[0]
        if sum(weights.values()) >= 50 and weight / sum(weights.values()) >= .985:
            return category
    return None


def profile_search_conditions(query: str) -> list:
    """Match a plausible name or all words in one specialty/category value.

    Imported names sometimes contain an entire role phrase. Match names as
    names, and use the source taxonomy for unambiguous profession searches.
    """
    # Two-character infix patterns have no useful trigrams and can scan the
    # whole multi-million-row directory; the licence filter handles RN, MD, etc.
    words = re.findall(r"\w+", query[:120])[:6]
    # Short names are common in candidate data. Search them as exact names so
    # a two-letter query never triggers an unindexed role substring scan.
    if len(words) == 1 and len(words[0]) == 2:
        name = words[0].lower()
        return [or_(
            func.lower(func.trim(Profile.first_name)) == name,
            func.lower(func.trim(Profile.last_name)) == name,
        )]
    if len(words) == 2 and any(len(word) == 2 for word in words):
        return [_NAME_VALUE == " ".join(word.lower() for word in words)]
    tokens = [word for word in words if len(word) >= 3]
    if not tokens:
        return [literal(False)]
    escaped = [token.lower().replace("_", "\\_") for token in tokens]
    patterns = [f"%{token}%" for token in escaped]
    normalized = " ".join(token.lower() for token in tokens)
    if len(tokens) == 1:
        name_match = or_(
            func.lower(func.trim(Profile.first_name)) == normalized,
            func.lower(func.trim(Profile.last_name)) == normalized,
        )
    else:
        name_match = _NAME_VALUE == normalized
    role_match = or_(*(and_(*(field.like(pattern, escape="\\")
                              for pattern in patterns)) for field in _ROLE_VALUES))
    category = _role_category(normalized)
    if category:
        role_match = and_(role_match, or_(
            func.lower(func.trim(Profile.job_category)) == category.casefold(),
            Profile.job_category.is_(None),
            func.trim(Profile.job_category) == "",
        ))
    # Keep the combined trigram-index predicate as a fast prefilter. The final
    # condition prevents words spread across unrelated fields from matching.
    return [
        *(_SEARCH_VALUE.like(pattern, escape="\\") for pattern in patterns),
        or_(name_match, role_match) if not category else role_match,
    ]
