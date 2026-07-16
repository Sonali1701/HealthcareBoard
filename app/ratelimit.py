"""Rate limiting via slowapi (in-memory by default).

The global default limit is applied to every route through SlowAPIMiddleware.
Tighter per-route limits (e.g. on login) use the ``limiter.limit`` decorator.
For multi-process / multi-host deployments, point ``storage_uri`` at Redis.
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from .config import settings

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.default_rate_limit] if settings.rate_limit_enabled else [],
    enabled=settings.rate_limit_enabled,
)
