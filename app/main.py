"""HealthBoard FastAPI application entry point.

Run locally:
    uvicorn app.main:app --reload
Interactive API docs:  http://127.0.0.1:8000/docs
The static HTML frontends are served from the project root at /ui/<file>.html
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text as sa_text
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi import _rate_limit_exceeded_handler

from . import __version__
from .bootstrap import ensure_admin
from .config import settings
from .database import SessionLocal, init_db
from .deps import CurrentUser, DbSession
from .ratelimit import limiter
from .routers import (
    admin,
    admin_import,
    analytics,
    applications,
    auth,
    clients,
    credits,
    duplicates,
    employers,
    extension,
    gsa,
    ingest,
    integrations,
    jobs,
    matching,
    messaging,
    notifications,
    outreach,
    pools,
    privacy,
    profiles,
    saved_searches,
    social,
    submissions,
    uploads,
)
from .web.core import RedirectException, _user_from_request, templates
from .web.routes import auth as web_auth
from .web.routes import matching as web_matching
from .web.routes import messages as web_messages
from .web.routes import public as web_public
from .web.routes import recruiter as web_recruiter
from .web.routes import seeker as web_seeker
from .web.routes import tools as web_tools

PROJECT_ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger("healthboard")


# Error monitoring: initialise Sentry as early as possible when a DSN is set, so
# unhandled exceptions are captured instead of vanishing into the logs. Guarded
# so a missing package or bad DSN can never stop the app from booting, and
# send_default_pii stays off — this app handles clinician PII.
if settings.sentry_dsn:
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.environment,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            send_default_pii=False,
        )
        logger.info("Sentry error monitoring enabled (env=%s)", settings.environment)
    except Exception:  # noqa: BLE001 — monitoring must never break startup
        logger.warning("Sentry initialisation failed; continuing without it", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_admin()
    yield


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description=(
        "Backend for the HealthBoard healthcare-staffing platform — auth, "
        "profiles, jobs, applications, social, messaging, AI matching and "
        "GSA per-diem pay calculation."
    ),
    lifespan=lifespan,
    # The interactive docs enumerate the whole API surface — keep them off in
    # production so they aren't a free map of every endpoint.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)

# Rate limiting (slowapi): register limiter, handler, and the enforcing middleware.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
if settings.rate_limit_enabled:
    app.add_middleware(SlowAPIMiddleware)

# Never combine a wildcard origin with credentials: the CORS spec forbids it,
# and Starlette would otherwise reflect any caller's Origin back, letting any
# website make credentialed cross-origin calls. With a wildcard, drop creds.
_cors_wildcard = settings.cors_origin_list == ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=not _cors_wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
    # So a split-origin frontend can still read the single-session sign-out signal.
    expose_headers=["X-Session-Superseded"],
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Baseline security headers on every response."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # Complements X-Frame-Options; safe (only restricts framing, not resources).
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    if settings.is_production:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response

# --- Web app: load current user onto request.state for templates ----------

@app.middleware("http")
async def attach_web_user(request: Request, call_next):
    request.state.web_user = None
    # Only bother for non-API HTML routes.
    if not request.url.path.startswith(("/api", "/assets", "/static", "/docs", "/openapi")):
        db = SessionLocal()
        try:
            request.state.web_user = _user_from_request(request, db)
        finally:
            db.close()
    return await call_next(request)


@app.middleware("http")
async def _no_cache_app_assets(request: Request, call_next):
    """Never let browsers cache the app HTML / JS / CSS — otherwise an old cached
    asset (e.g. from before a feature was added) breaks styling or behaviour."""
    response = await call_next(request)
    path = request.url.path
    if (path in _PROTOTYPE_ROUTES or path.startswith("/ui/") or path == "/"
            or path.startswith("/api/")   # API reads must never come from cache
            or (path.startswith(("/static/", "/assets/")) and path.endswith((".js", ".css", ".html")))):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.exception_handler(RedirectException)
async def _handle_redirect(request: Request, exc: RedirectException):
    return RedirectResponse(exc.url, status_code=303)


# --- API routers ----------------------------------------------------------
for module in (
    auth, profiles, employers, jobs, applications, social,
    messaging, notifications, matching, gsa, analytics, uploads,
    integrations, admin_import, ingest, extension, pools, saved_searches,
    duplicates, outreach, credits, privacy, submissions, clients, admin,
):
    app.include_router(module.router)

# --- Meta -----------------------------------------------------------------

# Cached headline stats. This endpoint is unauthenticated and hit by every
# anonymous visitor to the landing page, and each miss runs three full scans of
# the 600k-row profiles table — an easy way to hammer the database. A short
# in-process TTL means those scans run at most once every few minutes per
# worker, no matter how much traffic the signed-out page gets.
_stats_cache: dict = {"at": 0.0, "data": None}
_STATS_TTL_SECONDS = 300


@app.get("/api/public/stats", tags=["meta"])
def public_stats():
    """Headline figures for the signed-out page.

    Deliberately counts only what a visitor could verify by signing up: the
    screened, listable directory rather than every row ever imported. Cached for
    a few minutes — these totals barely move and don't need to be live.
    """
    import time

    now = time.monotonic()
    if _stats_cache["data"] is not None and (now - _stats_cache["at"]) < _STATS_TTL_SECONDS:
        return _stats_cache["data"]

    db = SessionLocal()
    try:
        providers = db.execute(sa_text(
            "SELECT count(*) FROM profiles WHERE is_listable IS TRUE")).scalar() or 0
        jobs = db.execute(sa_text(
            "SELECT count(*) FROM job_postings WHERE status = 'active'")).scalar() or 0
        # Constrained to real US state codes: the imported data carries junk
        # values, and a distinct count reported 61 "states".
        states = db.execute(sa_text(
            "SELECT count(DISTINCT upper(state_code)) FROM profiles "
            "WHERE is_listable IS TRUE AND upper(state_code) IN "
            "('AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL',"
            "'IN','IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT',"
            "'NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI',"
            "'SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY','DC')")).scalar() or 0
    except Exception:
        # Serve the last good value through a blip rather than a bare null.
        if _stats_cache["data"] is not None:
            return _stats_cache["data"]
        return {"providers": None, "jobs": None, "states": None}
    finally:
        db.close()

    result = {"providers": providers, "jobs": jobs, "states": states}
    _stats_cache["data"] = result
    _stats_cache["at"] = now
    return result


@app.get("/api/health", tags=["meta"])
def health():
    """Liveness + readiness. Probes the database so a deploy whose DB is down or
    whose schema drifted fails the check (503) instead of reporting a hollow
    'ok' — the previous version never touched the DB."""
    db = SessionLocal()
    try:
        db.execute(sa_text("SELECT 1"))
        db_ok = True
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        logger.error("health check: database probe failed: %s", exc)
    finally:
        db.close()

    payload = {
        "status": "ok" if db_ok else "degraded",
        "version": __version__,
        "env": settings.environment,
        "database": "ok" if db_ok else "error",
    }
    return payload if db_ok else JSONResponse(payload, status_code=503)


# --- Launch app UI --------------------------------------------------------

@app.get("/", include_in_schema=False)
def launch_board(request: Request):
    return templates.TemplateResponse("launch/board.html", {"request": request})


@app.get("/reset-password", include_in_schema=False)
def reset_password_page(request: Request):
    """Landing page for the link in a password-reset email (reads ?token=)."""
    return templates.TemplateResponse("auth/reset_password.html", {"request": request})


@app.get("/verify-email", include_in_schema=False)
def verify_email_page(request: Request):
    """Landing page for the link in an email-verification email (reads ?token=)."""
    return templates.TemplateResponse("auth/verify_email.html", {"request": request})


@app.get("/terms", include_in_schema=False)
def terms_page(request: Request):
    return templates.TemplateResponse("legal/terms.html", {"request": request})


@app.get("/privacy", include_in_schema=False)
def privacy_page(request: Request):
    return templates.TemplateResponse("legal/privacy.html", {"request": request})


@app.get("/match", include_in_schema=False)
def launch_match():
    return RedirectResponse("/")


@app.get("/chat", include_in_schema=False)
def launch_chat():
    return RedirectResponse("/")


@app.get("/calculator", include_in_schema=False)
def launch_calculator():
    return RedirectResponse("/?page=calculator")


# --- Prototype reference pages, not product routes ------------------------
_PROTOTYPE_ROUTES = {
    "/prototype/board": "healthboard-pro.html",
    "/prototype/match": "healthboard-ai-matching.html",
    "/prototype/chat": "healthboard-chat-platform.html",
    "/prototype/calculator": "healthboard-gsa-pay-calculator.html",
    "/prototype/calculator-lite": "healthboard-pay-calculator.html",
    "/prototype/schema": "healthboard-database-architecture.html",
}


def _make_page_route(filename: str):
    def _serve():
        return FileResponse(PROJECT_ROOT / filename)
    return _serve


for _path, _file in _PROTOTYPE_ROUTES.items():
    app.add_api_route(_path, _make_page_route(_file), include_in_schema=False)


@app.get("/ui/{page}", include_in_schema=False)
def serve_ui(page: str):
    candidate = (PROJECT_ROOT / page).resolve()
    if (candidate.parent == PROJECT_ROOT and candidate.suffix == ".html"
            and candidate.exists()):
        return FileResponse(candidate)
    return RedirectResponse(url="/")


def _authorized_for_file(db, user, key: str) -> bool:
    """These files are résumés and profile photos — PII. Being logged in is not
    enough: you may fetch a file only if it belongs to a provider you own, a
    provider whose contact you have released (paid to reveal), a candidate who
    applied to your job, or if you are the platform admin. Unknown keys are
    refused so this endpoint can't be probed."""
    from sqlalchemy import or_, select

    from .models import Profile
    from .routers.profiles import _entitled_to_resume

    if getattr(user.role, "value", None) == "admin":
        return True
    # The key is the URL suffix in every storage form (/files/<k>,
    # /static/uploads/<k>, <public-base>/<k>). Escape LIKE wildcards so a key
    # can never be turned into a pattern.
    esc = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pat = f"%{esc}"
    prof = db.scalar(select(Profile).where(or_(
        Profile.resume_url.like(pat, escape="\\"),
        Profile.profile_photo_url.like(pat, escape="\\"),
    )))
    if not prof:
        return False
    return _entitled_to_resume(db, user, prof)


@app.get("/files/{key:path}", include_in_schema=False)
def serve_file(key: str, user: CurrentUser, db: DbSession):
    """Serve a stored file. Requires authentication AND object-level
    authorization — these are résumés and other PII, and this used to hand out a
    signed URL to any logged-in user. For a private S3/R2 bucket it redirects to
    a short-lived signed URL; locally it points at the upload fallback."""
    if not _authorized_for_file(db, user, key):
        # Don't disclose whether the key exists.
        raise HTTPException(status_code=404, detail="Not found")
    if settings.storage_enabled:
        from .services import storage
        return RedirectResponse(storage.presigned_url(key))
    return RedirectResponse(f"/static/uploads/{key}")


# --- Jinja web app (kept available under /app/* for the simpler UI) --------
for module in (web_public, web_auth, web_seeker, web_recruiter,
               web_matching, web_messages, web_tools):
    app.include_router(module.router, prefix="/app")


# --- Static assets --------------------------------------------------------
# /assets -> app design assets (css/js). We deliberately do NOT mount the
# project root: doing so served .env, the database and every résumé over HTTP.
# Only the local upload fallback directory is exposed, at /static/uploads.
app.mount("/assets", StaticFiles(directory=str(PROJECT_ROOT / "static")), name="assets")
app.mount("/static/uploads",
          StaticFiles(directory=str(PROJECT_ROOT / "uploads"), check_dir=False),
          name="local-uploads")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Browsers and link unfurlers ask for /favicon.ico regardless of the
    <link rel="icon"> tag; without this every page load logged a 404."""
    return FileResponse(PROJECT_ROOT / "static" / "favicon.svg",
                        media_type="image/svg+xml")
