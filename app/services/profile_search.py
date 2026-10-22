"""Search provider identity and taxonomy fields without stale search_text."""
from __future__ import annotations

import re

from sqlalchemy import func, literal, literal_column

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


def profile_search_conditions(query: str) -> list:
    """Require each word somewhere in the four provider search fields."""
    # Two-character infix patterns have no useful trigrams and can scan the
    # whole multi-million-row directory; the licence filter handles RN, MD, etc.
    tokens = [word for word in re.findall(r"\w+", query[:120]) if len(word) >= 3][:6]
    if not tokens:
        return [literal(False)]
    return [_SEARCH_VALUE.like(f"%{token.lower()}%", escape="\\")
            for token in tokens]
