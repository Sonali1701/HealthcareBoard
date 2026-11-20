"""Shared Neon connection guard for the resume cleanup tools."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse


def database_url(env_file: Path | None = None) -> str:
    if env_file:
        value = None
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            if line.lstrip().startswith("DATABASE_URL="):
                value = line.split("=", 1)[1].strip().strip('"\'')
                break
    else:
        value = os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL is required")
    url = value.replace("postgresql+psycopg://", "postgresql://", 1)
    if urlparse(url).path != "/neondb":
        raise RuntimeError("Refusing to access a database other than neondb")
    return url
