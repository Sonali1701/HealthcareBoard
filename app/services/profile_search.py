"""Search provider identity and taxonomy fields without stale search_text."""
from __future__ import annotations

import re

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
    _NAME_VALUE,
    func.lower(func.coalesce(Profile.specialty, _EMPTY)),
    func.lower(func.coalesce(Profile.job_category, _EMPTY)),
)


def profile_search_conditions(query: str) -> list:
    """Match all words in one name, specialty, or category value.

    A phrase such as "nurse practitioner" must not match "nurse" in a
    physician's name and "practitioner" in another field.
    """
    # Two-character infix patterns have no useful trigrams and can scan the
    # whole multi-million-row directory; the licence filter handles RN, MD, etc.
    tokens = [word for word in re.findall(r"\w+", query[:120]) if len(word) >= 3][:6]
    if not tokens:
        return [literal(False)]
    escaped = [token.lower().replace("_", "\\_") for token in tokens]
    patterns = [f"%{token}%" for token in escaped]
    # Keep the combined trigram-index predicate as a fast prefilter. The final
    # condition prevents words spread across unrelated fields from matching.
    return [
        *(_SEARCH_VALUE.like(pattern, escape="\\") for pattern in patterns),
        or_(*(and_(*(field.like(pattern, escape="\\") for pattern in patterns))
               for field in _ROLE_VALUES)),
    ]
