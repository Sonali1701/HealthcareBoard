"""HealthBoard FastAPI application entry point.

Run locally:
    uvicorn app.main:app --reload
Interactive API docs:  http://127.0.0.1:8000/docs
The static HTML frontends are served from the project root at /ui/<file>.html
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi import _rate_limit_exceeded_handler

from . import __version__
from .bootstrap import ensure_admin
from .config import settings
from .database import SessionLocal, init_db
from .deps import CurrentUser
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

@app.get("/api/public/stats", tags=["meta"])
def public_stats():
    """Headline figures for the signed-out page.

    Deliberately counts only what a visitor could verify by signing up: the
    screened, listable directory rather than every row ever imported.
    """
    from sqlalchemy import text as _t

    db = SessionLocal()
    try:
        providers = db.execute(_t(
            "SELECT count(*) FROM profiles WHERE is_listable IS TRUE")).scalar() or 0
        jobs = db.execute(_t(
            "SELECT count(*) FROM job_postings WHERE status = 'active'")).scalar() or 0
        # Constrained to real US state codes: the imported data carries junk
        # values, and a distinct count reported 61 "states".
        states = db.execute(_t(
            "SELECT count(DISTINCT upper(state_code)) FROM profiles "
            "WHERE is_listable IS TRUE AND upper(state_code) IN "
            "('AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL',"
            "'IN','IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT',"
            "'NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI',"
            "'SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY','DC')")).scalar() or 0
    except Exception:
        return {"providers": None, "jobs": None, "states": None}
    finally:
        db.close()
    return {"providers": providers, "jobs": jobs, "states": states}


@app.get("/api/health", tags=["meta"])
def health():
    return {"status": "ok", "version": __version__, "env": settings.environment}


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


@app.get("/files/{key:path}", include_in_schema=False)
def serve_file(key: str, user: CurrentUser):
    """Serve a stored file. Requires authentication — these are résumés and
    other PII, and this used to hand out signed URLs to anyone. For a private
    S3/R2 bucket this redirects to a short-lived signed URL; locally it points
    at the upload fallback. (Keys are unguessable UUIDs.)"""
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
