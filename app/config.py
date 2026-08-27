"""Application configuration loaded from environment / .env file."""
from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Core ---
    app_name: str = "HealthBoard API"
    environment: str = "development"
    debug: bool = True

    # --- Error monitoring (Sentry) ---
    # Empty by default: no data leaves the box until a DSN is set. In production
    # set SENTRY_DSN so unhandled errors are captured instead of vanishing.
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.0   # 0 = errors only, no perf tracing

    # --- Database ---
    # SQLite for local dev; swap for a postgres URL in production (code is portable).
    database_url: str = "sqlite:///./healthboard.db"

    # --- Auth / JWT ---
    # CHANGE THIS in production (set JWT_SECRET env var).
    jwt_secret: str = "dev-only-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 30

    # --- Single active session (anti account-sharing) ---
    # When on, logging in on a new device signs the account out everywhere else,
    # so one paid seat can't be used by several people at once. Scoped to the
    # paid/agency roles by default; set to "*" to enforce for every account, or a
    # comma-separated list of roles (e.g. "recruiter,employer,admin").
    single_session_enabled: bool = True
    single_session_roles: str = "recruiter,employer"

    # --- CORS ---
    # Comma-separated origins, or "*" for all (dev default).
    cors_origins: str = "*"

    # --- External integrations ---
    # Free key from https://api.data.gov/signup ; kept server-side so the
    # frontend never exposes it. If empty, GSA proxy serves fallback rates.
    gsa_api_key: str = ""
    gsa_base_url: str = "https://api.gsa.gov/travel/perdiem/v2"
    # Keep the deployment override in sync when GSA publishes a new fiscal year.
    gsa_fiscal_year: int = 2026

    # --- Credits ---
    # One credit per candidate, spent when their contact is revealed. Nothing
    # else is metered: once a recruiter has paid for a candidate, emailing them
    # or reading their résumé must not bill for the same person again.
    credits_enabled: bool = True
    credit_signup_bonus: int = 25          # starting balance for a new recruiter
    credit_cost_reveal_contact: int = 1

    # --- Licence verification ---
    # "unavailable" (default) reports honestly that nothing checked the licence.
    # "manual" records a recruiter's own check against the board. A real source
    # (Nursys, a state board) plugs in via services.license_verify.register().
    license_verify_provider: str = "unavailable"
    license_verify_api_key: str = ""

    # --- Email (SendGrid) ---
    # When email_enabled is False, reset/verification tokens are returned in the
    # API response (dev convenience) instead of being emailed.
    email_enabled: bool = False
    sendgrid_api_key: str = ""
    email_from: str = "no-reply@healthboard.dev"
    email_from_name: str = "HealthBoard"
    # Base URL the frontend is served from — used to build reset/verify links.
    frontend_base_url: str = "http://127.0.0.1:8000"

    # --- Nexus / LaborEdge job feed ---
    # Pulls open reqs from the agency's LaborEdge (Nexus) ATS into our job
    # board. LaborEdge issues the credentials — drop them into the environment
    # to switch the sync on; with nexus_enabled False the sync is a no-op.
    # The Basic-auth value is the fixed client credential from the API docs.
    nexus_enabled: bool = False
    nexus_token_url: str = "https://api-nexus.laboredge.com/auth/oauth2/token"
    nexus_base_url: str = "https://api-nexus.laboredge.com:9000"
    nexus_basic_auth: str = "bmV4dXM6NXM6Nn5EcEhaelcmVFoj"  # "basic <this>"
    nexus_username: str = ""
    nexus_password: str = ""
    nexus_org_code: str = ""          # e.g. "FSM"
    nexus_grant_type: str = "password"

    # --- Payments (Stripe) ---
    # When payments_enabled is False (or no key), credit purchases are turned
    # off and the buy button explains that to the recruiter. Set the keys from
    # the Stripe dashboard to switch it on. The webhook secret verifies that a
    # completion callback genuinely came from Stripe before granting credits.
    payments_enabled: bool = False
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    # --- Object storage (S3-compatible: Vultr / AWS S3 / Cloudflare R2) ---
    # When storage_enabled is False, uploads fall back to ./uploads served at /static.
    storage_enabled: bool = False
    s3_endpoint_url: str = ""        # e.g. https://ewr1.vultrobjects.com
    s3_region: str = "us-east-1"
    s3_bucket: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_public_base_url: str = ""     # CDN/public base; defaults to endpoint/bucket
    # ACL to apply on upload. Use "public-read" for AWS S3; leave EMPTY for
    # Cloudflare R2 (R2 rejects ACLs).
    s3_acl: str = ""
    # False (default, recommended for PII) = private bucket; files are served via
    # short-lived signed URLs through /files/<key>. True = public bucket, direct
    # URLs from S3_PUBLIC_BASE_URL.
    s3_public: bool = False
    s3_signed_url_ttl: int = 3600  # seconds a signed résumé link stays valid

    # --- Admin bootstrap (created on startup if both are set) ---
    admin_email: str = ""
    admin_password: str = ""

    # --- Rate limiting ---
    rate_limit_enabled: bool = True
    default_rate_limit: str = "240/minute"
    auth_rate_limit: str = "20/minute"

    # --- LLM extraction (OpenAI-compatible: OpenAI / Ollama / Groq / DeepSeek) ---
    llm_enabled: bool = False
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout: float = 60.0
    # Minimum seconds between LLM calls, to stay under provider rate limits.
    # Gemini free tier = 20 req/min, so ~3.5s keeps us safely under. Set 0 for
    # paid tiers with high limits.
    llm_min_interval: float = 0.0
    # A paid Gemini key can be supplied under its natural name; when present it
    # takes precedence over LLM_API_KEY for all LLM calls (see validator below).
    gemini_api_key: str = ""

    # --- Bulk résumé import endpoint ---
    # Shared secret the trusted uploader sends as the X-Import-Token header on
    # POST /api/admin/import. Empty (default) DISABLES the endpoint entirely, so
    # it's inert until you deliberately set a long random value in .env.
    import_api_token: str = ""

    # --- Ceipal ATS integration ---
    ceipal_enabled: bool = False
    ceipal_base_url: str = "https://api.ceipal.com"
    ceipal_auth_url: str = "https://api.ceipal.com/v2/createAuthtoken/"
    ceipal_api_key: str = ""
    ceipal_email: str = ""
    ceipal_password: str = ""
    # The "get-report-data" URL from the Ceipal Custom Report (the jobs report).
    ceipal_report_url: str = ""

    @model_validator(mode="after")
    def _prefer_gemini_key(self):
        """If GEMINI_API_KEY is set, use it as the LLM key (paid key wins)."""
        if self.gemini_api_key:
            self.llm_api_key = self.gemini_api_key
        return self

    @field_validator("debug", mode="before")
    @classmethod
    def _coerce_debug(cls, v):
        """Accept release/prod-style values as debug=False."""
        if isinstance(v, str) and v.strip().lower() in {
            "release",
            "prod",
            "production",
        }:
            return False
        return v

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        """Rewrite plain postgres URLs (e.g. Render's) to use the psycopg driver."""
        if v.startswith("postgres://"):
            return "postgresql+psycopg://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            return "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        """True for production-like environments (robust to 'prod'/'release')."""
        return self.environment.strip().lower() in {"production", "prod", "release"}

    def enforces_single_session(self, role_value: str) -> bool:
        """Whether single-active-session is enforced for a user of this role."""
        if not self.single_session_enabled:
            return False
        roles = {r.strip() for r in self.single_session_roles.split(",") if r.strip()}
        return "*" in roles or role_value in roles

    @model_validator(mode="after")
    def _guard_production(self):
        """Refuse to boot in production with insecure defaults, rather than
        silently shipping a forgeable JWT secret or an ephemeral SQLite file."""
        if self.is_production:
            if self.jwt_secret == "dev-only-insecure-secret-change-me":
                raise ValueError("Set a strong JWT_SECRET in production")
            if self.database_url.startswith("sqlite"):
                raise ValueError(
                    "Set a PostgreSQL DATABASE_URL in production (SQLite is not supported)")
        return self

    @property
    def storage_public_base(self) -> str:
        if self.s3_public_base_url:
            return self.s3_public_base_url.rstrip("/")
        if self.s3_endpoint_url and self.s3_bucket:
            return f"{self.s3_endpoint_url.rstrip('/')}/{self.s3_bucket}"
        return ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
