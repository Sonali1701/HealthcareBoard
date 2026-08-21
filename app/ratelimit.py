"""Rate limiting.

The global default limit is applied to every route through SlowAPIMiddleware.
Auth endpoints get a much tighter per-IP limit via the ``auth_rate_limit``
dependency below (a small sliding window). Both are in-memory / per-process;
for multi-process or multi-host deployments this should move to Redis.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status
from slowapi import Limiter
from slowapi.util import get_remote_address

from .config import settings

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.default_rate_limit] if settings.rate_limit_enabled else [],
    enabled=settings.rate_limit_enabled,
)

_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


def _parse_rate(rate: str) -> tuple[int, int]:
    """'20/minute' -> (20, 60). Falls back to 20/minute on anything unexpected."""
    try:
        count, unit = rate.split("/")
        unit = unit.strip().lower().rstrip("s")   # "minutes" -> "minute"
        return int(count), _UNIT_SECONDS[unit]
    except Exception:  # noqa: BLE001
        return 20, 60


# Per-IP hit timestamps for the auth limiter. Deques are pruned on each check,
# and empty ones are dropped, so this stays bounded under normal traffic.
_auth_hits: dict[str, deque] = defaultdict(deque)


def auth_rate_limit(request: Request) -> None:
    """FastAPI dependency: tight per-IP limit for authentication endpoints.

    Applied to login/register/password-reset/MFA so those can't be brute-forced,
    independent of the looser global limit. Uses a real dependency (not the
    slowapi decorator, which mangles the endpoint signature under FastAPI).
    """
    if not settings.rate_limit_enabled:
        return
    count, window = _parse_rate(settings.auth_rate_limit)
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    hits = _auth_hits[ip]
    while hits and now - hits[0] > window:
        hits.popleft()
    if len(hits) >= count:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Please wait a minute and try again.")
    hits.append(now)
    if not hits:
        _auth_hits.pop(ip, None)
