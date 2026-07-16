"""File storage abstraction.

Uses S3-compatible object storage (Vultr Object Storage / AWS S3 / Cloudflare R2)
via boto3 when configured. Falls back to local disk (./uploads, served at
/static/uploads) when ``settings.storage_enabled`` is False, so uploads work in
local dev without any cloud account.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlparse

from ..config import settings

logger = logging.getLogger("healthboard.storage")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LOCAL_UPLOAD_DIR = PROJECT_ROOT / "uploads"

_s3_client = None


def _client():
    global _s3_client
    if _s3_client is None:
        import boto3

        _s3_client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url or None,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
        )
    return _s3_client


def build_key(prefix: str, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return f"{prefix}/{uuid.uuid4().hex}{suffix}"


def _local_path(key: str) -> Path:
    """Resolve a storage key inside the local uploads directory."""
    root = LOCAL_UPLOAD_DIR.resolve()
    path = (root / key.replace("\\", "/")).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Invalid storage key") from exc
    return path


def key_from_url(url: str) -> tuple[str, bool]:
    """Return (storage key, is_local_upload_url) for a stored file URL."""
    raw = (url or "").strip()
    parsed = urlparse(raw)
    path = parsed.path or raw

    if path.startswith("/files/"):
        return path[len("/files/"):].lstrip("/"), False
    if "/static/uploads/" in path:
        return path.split("/static/uploads/", 1)[1].lstrip("/"), True
    if path.startswith("/uploads/"):
        return path[len("/uploads/"):].lstrip("/"), True

    public_base = settings.storage_public_base
    if public_base and raw.startswith(public_base.rstrip("/") + "/"):
        return raw[len(public_base.rstrip("/")) + 1:].split("?", 1)[0], False
    if ".r2.dev/" in raw:
        return raw.split(".r2.dev/", 1)[1].split("?", 1)[0], False

    return path.lstrip("/"), False


def upload(fileobj: BinaryIO, key: str, content_type: str) -> str:
    """Store a file and return its public URL."""
    if settings.storage_enabled:
        extra = {"ContentType": content_type}
        if settings.s3_acl:  # omitted for Cloudflare R2; set for AWS S3
            extra["ACL"] = settings.s3_acl
        _client().upload_fileobj(fileobj, settings.s3_bucket, key, ExtraArgs=extra)
        if settings.s3_public and settings.storage_public_base:
            return f"{settings.storage_public_base}/{key}"
        # Private bucket (recommended for PII): serve via the signed-URL redirect.
        return f"/files/{key}"

    # Local fallback
    dest = LOCAL_UPLOAD_DIR / key
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        fileobj.seek(0)
        f.write(fileobj.read())
    return f"/static/uploads/{key}"


def presigned_url(key: str, expires: int | None = None) -> str:
    """Short-lived GET URL for a private object."""
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": key},
        ExpiresIn=expires or settings.s3_signed_url_ttl,
    )


def download_bytes(key: str, *, prefer_local: bool = False) -> bytes:
    """Read a stored object's raw bytes (for server-side rendering)."""
    if prefer_local:
        path = _local_path(key)
        if path.exists():
            with open(path, "rb") as f:
                return f.read()
    if settings.storage_enabled:
        obj = _client().get_object(Bucket=settings.s3_bucket, Key=key)
        return obj["Body"].read()
    with open(_local_path(key), "rb") as f:
        return f.read()
