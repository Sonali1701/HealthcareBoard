"""Push existing local files (./uploads) to your S3/Cloudflare R2 bucket and
rewrite the resume_url / profile_photo_url values in the database to the bucket.

Run AFTER setting STORAGE_ENABLED=true and the S3_* values in .env:
    .venv\\Scripts\\python -m app.migrate_uploads_to_r2

It uploads each file under its existing key (so paths are preserved) and updates
any DB URL that points at /static/uploads/<key> or /uploads/<key>. Runs against
whatever DATABASE_URL is set to (SQLite or Postgres).
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from sqlalchemy import select

from .config import settings
from .database import SessionLocal
from .models import Profile
from .services import storage


def main() -> None:
    if not settings.storage_enabled:
        print("STORAGE_ENABLED is false — set it true + the S3_* creds in .env first.")
        return

    root = storage.LOCAL_UPLOAD_DIR
    if not root.exists():
        print(f"No local uploads directory at {root} — nothing to migrate.")
        return

    # 1) Upload every local file under its existing key (preserves the path).
    uploaded: dict[str, str] = {}
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        key = str(f.relative_to(root)).replace("\\", "/")
        ct = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        with open(f, "rb") as fh:
            uploaded[key] = storage.upload(fh, key, ct)
        print(f"  uploaded  {key}")

    # 2) Rewrite DB URLs that point at the old local paths.
    db = SessionLocal()
    changed = 0
    try:
        for p in db.scalars(select(Profile)):
            for attr in ("resume_url", "profile_photo_url"):
                v = getattr(p, attr)
                if v and "/uploads/" in v and not v.startswith("http"):
                    key = v.split("/uploads/", 1)[1]
                    if key in uploaded:
                        setattr(p, attr, uploaded[key])
                        changed += 1
        db.commit()
    finally:
        db.close()

    print(f"\nDone — uploaded {len(uploaded)} file(s), updated {changed} DB URL(s).")


if __name__ == "__main__":
    main()
