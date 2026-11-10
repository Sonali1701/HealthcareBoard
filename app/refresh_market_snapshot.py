"""Refresh the durable Analytics market snapshot after an import or migration."""
from __future__ import annotations

import logging

from .database import SessionLocal
from .routers.analytics import refresh_market_snapshot


def main() -> None:
    with SessionLocal() as db:
        refreshed = refresh_market_snapshot(db, force=True, blocking=True)
    logging.info("Analytics market snapshot refreshed: %s", refreshed)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
