"""Private Cloudflare R2 storage for candidate resume PDFs."""
from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from . import config


def enabled() -> bool:
    return bool(config.STORAGE_ENABLED)


def _settings() -> dict:
    values = {
        "endpoint_url": config.S3_ENDPOINT_URL,
        "access_key": config.S3_ACCESS_KEY,
        "secret_key": config.S3_SECRET_KEY,
        "bucket": config.S3_BUCKET,
        "region": config.S3_REGION,
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(
            "Resume object storage is enabled but missing: " + ", ".join(missing)
        )
    return values


@lru_cache(maxsize=1)
def _client():
    settings = _settings()
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - explicit dependency error
        raise RuntimeError(
            "Cloud resume storage is enabled but boto3 is not installed. "
            "Run: pip install -r requirements.txt"
        ) from exc
    return boto3.client(
        "s3",
        endpoint_url=settings["endpoint_url"],
        aws_access_key_id=settings["access_key"],
        aws_secret_access_key=settings["secret_key"],
        region_name=settings["region"],
        config=Config(
            connect_timeout=10,
            read_timeout=120,
            retries={"max_attempts": 2, "mode": "standard"},
            tcp_keepalive=True,
        ),
    )


def _safe_filename(filename: str) -> str:
    name = Path(filename or "resume.pdf").name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    if not stem.lower().endswith(".pdf"):
        stem = f"{stem or 'resume'}.pdf"
    return stem[:180]


def upload_resume(candidate_id: int, filename: str, data: bytes) -> dict:
    """Upload one PDF and return safe metadata for the database."""
    settings = _settings()
    digest = hashlib.sha256(data).hexdigest()
    safe_name = _safe_filename(filename)
    object_key = f"resumes/{int(candidate_id)}/{digest[:20]}-{safe_name}"
    response = _client().put_object(
        Bucket=settings["bucket"],
        Key=object_key,
        Body=data,
        ContentType="application/pdf",
        Metadata={
            "candidate-id": str(int(candidate_id)),
            "sha256": digest,
        },
    )
    public_url = ""
    if config.S3_PUBLIC_BASE_URL:
        public_url = f"{config.S3_PUBLIC_BASE_URL}/{quote(object_key, safe='/')}"
    return {
        "storage_provider": "r2",
        "object_key": object_key,
        "bucket": settings["bucket"],
        "public_url": public_url,
        "checksum_sha256": digest,
        "etag": str(response.get("ETag") or "").strip('"'),
    }


def download_resume(object_key: str) -> bytes:
    if not object_key:
        raise ValueError("resume object key is missing")
    settings = _settings()
    response = _client().get_object(Bucket=settings["bucket"], Key=object_key)
    return response["Body"].read()
