"""Web layer plumbing: templates, cookie sessions, flash messages, auth deps."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import jwt
from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import User, UserStatus
from ..security import ACCESS_TOKEN, decode_token

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))

SESSION_COOKIE = "hb_session"
FLASH_COOKIE = "hb_flash"
_flash_signer = URLSafeSerializer(settings.jwt_secret, salt="hb-flash")

# Template globals
templates.env.globals["app_name"] = "HealthBoard"


# --- Redirect-based auth guard --------------------------------------------

class RedirectException(Exception):
    def __init__(self, url: str):
        self.url = url


# --- Session cookie helpers ------------------------------------------------

def set_session(resp, token: str) -> None:
    resp.set_cookie(
        SESSION_COOKIE, token,
        httponly=True, samesite="lax",
        # Robust across "prod"/"release"; a plain == "production" check would
        # silently ship the session cookie over plaintext HTTP otherwise.
        secure=settings.is_production,
        max_age=settings.refresh_token_expire_days * 86400,
        path="/",
    )


def clear_session(resp) -> None:
    resp.delete_cookie(SESSION_COOKIE, path="/")


def _user_from_request(request: Request, db: Session) -> Optional[User]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        return None
    if payload.get("type") != ACCESS_TOKEN:
        return None
    user = db.get(User, payload.get("sub"))
    if not user or user.deleted_at is not None or user.status == UserStatus.suspended:
        return None
    return user


# --- Dependencies ----------------------------------------------------------

def current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    return _user_from_request(request, db)


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = _user_from_request(request, db)
    if not user:
        raise RedirectException(f"/login?next={request.url.path}")
    return user


# --- Flash messages (signed cookie, read-once) -----------------------------

def _set_flash(resp, message: str, kind: str = "success") -> None:
    resp.set_cookie(FLASH_COOKIE, _flash_signer.dumps({"m": message, "k": kind}),
                    httponly=True, samesite="lax", max_age=30, path="/")


def _pop_flash(request: Request):
    raw = request.cookies.get(FLASH_COOKIE)
    if not raw:
        return None
    try:
        return _flash_signer.loads(raw)
    except BadSignature:
        return None


def redirect(url: str, flash: str | None = None, kind: str = "success", status: int = 303):
    resp = RedirectResponse(url, status_code=status)
    if flash:
        _set_flash(resp, flash, kind)
    return resp


def render(request: Request, template: str, context: dict | None = None,
           status_code: int = 200, db: Session | None = None):
    """Render a template with request, current_user, flash, settings in context."""
    ctx = dict(context or {})
    ctx["request"] = request
    if "user" not in ctx:
        ctx["user"] = getattr(request.state, "web_user", None)
    flash = _pop_flash(request)
    ctx["flash"] = flash
    resp = templates.TemplateResponse(template, ctx, status_code=status_code)
    if flash:
        resp.delete_cookie(FLASH_COOKIE, path="/")
    return resp
