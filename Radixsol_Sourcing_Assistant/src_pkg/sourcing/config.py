"""
Configuration for the Radixsol Sourcing Assistant.

Enformion credentials, LLM settings, and compliance defaults live here. The
extension also supports user-triggered USPhoneBook browser lookup for candidate
names the recruiter is authorized to process.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from dotenv import load_dotenv

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
_ENV_LOCAL_FILE = Path(__file__).resolve().parents[2] / ".env.local"
load_dotenv(_ENV_FILE, override=False)
load_dotenv(_ENV_LOCAL_FILE, override=True)

# ---- Enformion / Endato API ----
ENFORMION_URL = os.getenv("ENFORMION_URL", "https://devapi.endato.com/Contact/Enrich")
ENFORMION_AP_NAME = os.getenv("ENFORMION_AP_NAME", "")
ENFORMION_AP_PASSWORD = os.getenv("ENFORMION_AP_PASSWORD", "")
ENFORMION_SEARCH_TYPE = os.getenv("ENFORMION_SEARCH_TYPE", "DevAPIContactEnrich")
HTTP_TIMEOUT = float(os.getenv("ENFORMION_HTTP_TIMEOUT", "20"))

# Demo mode returns deterministic mock enrichment when no key is set OR when
# ENFORMION_DEMO=1 — lets you run the whole product before wiring the real key.
DEMO_MODE = os.getenv("ENFORMION_DEMO", "").strip() in ("1", "true", "yes") or not ENFORMION_AP_NAME

# ---- LLM (Gemini) for outreach drafting ----
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
AI_MATCH_ENABLED = os.getenv("AI_MATCH_ENABLED", "").strip().lower() in (
    "1", "true", "yes",
)
IDENTITY_MATCH_THRESHOLD = float(os.getenv("IDENTITY_MATCH_THRESHOLD", "0.72"))

# ---- Optional deliverability / phone-line validation ----
VERIFY_EMAILS = os.getenv("VERIFY_EMAILS", "").strip().lower() in ("1", "true", "yes")
NEVERBOUNCE_API_KEY = os.getenv("NEVERBOUNCE_API_KEY", "")
VERIFY_PHONES = os.getenv("VERIFY_PHONES", "").strip().lower() in ("1", "true", "yes")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_API_KEY = os.getenv("TWILIO_API_KEY", "")
TWILIO_API_KEY_SECRET = os.getenv("TWILIO_API_KEY_SECRET", "")
DEFAULT_PHONE_COUNTRY = os.getenv("DEFAULT_PHONE_COUNTRY", "US")

# ---- Storage ----
DATABASE_BACKEND = os.getenv("DATABASE_BACKEND", "").strip().lower()
DATABASE_NAME = os.getenv("DATABASE_NAME", "").strip()
DATABASE_URL = (
    ""
    if DATABASE_BACKEND == "sqlite"
    else os.getenv("DATABASE_URL", "").strip()
)
if DATABASE_URL and DATABASE_NAME:
    if not re.fullmatch(r"[A-Za-z0-9_]+", DATABASE_NAME):
        raise RuntimeError("DATABASE_NAME may contain only letters, numbers, and underscores.")
    parsed_database_url = urlsplit(DATABASE_URL)
    DATABASE_URL = urlunsplit(parsed_database_url._replace(path=f"/{quote(DATABASE_NAME)}"))
DB_PATH = os.getenv("SOURCING_DB", "sourcing.db")
RESUME_DOWNLOAD_DIR = Path(
    os.getenv("RESUME_DOWNLOAD_DIR", str(Path.home() / "Downloads"))
).resolve()
RESUME_MAX_BYTES = int(os.getenv("RESUME_MAX_BYTES", str(15 * 1024 * 1024)))

# Resume object storage. R2 exposes an S3-compatible API; keep this disabled
# until S3_BUCKET points at a bucket dedicated to this application.
STORAGE_ENABLED = os.getenv("STORAGE_ENABLED", "").strip().lower() in (
    "1", "true", "yes",
)
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "").strip()
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "").strip()
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "").strip()
S3_BUCKET = os.getenv("S3_BUCKET", "").strip()
S3_REGION = os.getenv("S3_REGION", "auto").strip() or "auto"
S3_PUBLIC_BASE_URL = os.getenv("S3_PUBLIC_BASE_URL", "").strip().rstrip("/")

# ---- Compliance defaults (baked into the workflow) ----
# Default outreach channel; phone/SMS require extra consent (TCPA), so email-first.
DEFAULT_CHANNEL = "email"
REQUIRE_HUMAN_APPROVAL = True   # nothing sends automatically
HONOR_DNC = True                # do-not-contact / opt-out list is always enforced

COMPLIANCE_NOTICE = (
    "Contact data may come from licensed Enformion results or user-triggered "
    "public-directory lookup. Use for legitimate "
    "recruiting outreach only. Email sends must comply with CAN-SPAM (identify sender, "
    "honor opt-outs); phone/SMS outreach is subject to TCPA consent rules. This tool "
    "defaults to email, requires human approval before sending, and enforces a "
    "do-not-contact list."
)
