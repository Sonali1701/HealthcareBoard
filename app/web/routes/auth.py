"""Form-based auth for the web app: signup, login, logout."""
from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Form, Request
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import settings
from ...database import get_db, new_uuid, utcnow
from ...models import EmailVerificationToken, Profile, User, UserStatus
from ...models.enums import ProfileSource, UserRole
from ...security import (create_web_session_token, generate_opaque_token,
                         hash_password, sha256, verify_password)
from ...services.email import send_email_verification
from ...services.session_control import activate_session
from ..core import clear_session, current_user, redirect, render, set_session

router = APIRouter(tags=["web-auth"])

DbDep = Annotated[Session, Depends(get_db)]
_email_adapter = TypeAdapter(EmailStr)


@router.get("/signup")
def signup_form(request: Request, user=Depends(current_user)):
    if user:
        return redirect("/")
    return render(request, "auth/signup.html", {"active": "signup"})


@router.post("/signup")
def signup(
    request: Request,
    db: DbDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    role: Annotated[str, Form()] = "job_seeker",
    first_name: Annotated[str, Form()] = "",
    last_name: Annotated[str, Form()] = "",
):
    email = email.strip().lower()
    err = None
    try:
        email = str(_email_adapter.validate_python(email)).lower()
    except ValidationError:
        err = "Enter a valid email address."
    if not err and not 8 <= len(password) <= 128:
        err = "Password must be between 8 and 128 characters."
    elif not err and db.scalar(select(User).where(User.email == email)):
        err = "That email is already registered."
    if err:
        return render(request, "auth/signup.html",
                      {"active": "signup", "error": err, "email": email,
                       "first_name": first_name, "last_name": last_name, "role": role},
                      status_code=400)

    if settings.email_enabled and not settings.sendgrid_api_key.strip():
        return render(request, "auth/signup.html", {
            "active": "signup", "error": "Email verification is temporarily unavailable. Please try again later.",
            "email": email, "first_name": first_name,
            "last_name": last_name, "role": role,
        }, status_code=503)

    user_role = UserRole.recruiter if role == "recruiter" else UserRole.job_seeker
    needs_verification = settings.email_enabled
    user = User(email=email, password_hash=hash_password(password), role=user_role,
                status=UserStatus.pending_verify if needs_verification else UserStatus.active,
                email_verified_at=None if needs_verification else utcnow())
    db.add(user)
    db.flush()
    if user_role == UserRole.job_seeker:
        profile = Profile(user_id=user.user_id,
                          first_name=first_name.strip() or "New",
                          last_name=last_name.strip() or "Member",
                          source=ProfileSource.signup)
        profile.rebuild_search_text()
        db.add(profile)
    if needs_verification:
        raw_verify = generate_opaque_token()
        db.add(EmailVerificationToken(
            user_id=user.user_id, token_hash=sha256(raw_verify),
            expires_at=utcnow() + timedelta(days=2),
        ))
    else:
        session_id = new_uuid()
        activate_session(db, user, session_id)
    db.commit()

    if needs_verification:
        send_email_verification(user.email, raw_verify)
        return render(request, "auth/signup.html", {
            "active": "signup", "success": "Check your email for a verification link before signing in."
        })

    dest = "/recruiter" if user_role == UserRole.recruiter else "/dashboard"
    resp = redirect(dest, flash=f"Welcome to MedHunt, {first_name or email}!")
    set_session(resp, create_web_session_token(user.user_id, user.role.value, session_id=session_id))
    return resp


@router.get("/login")
def login_form(request: Request, next: Optional[str] = None, user=Depends(current_user)):
    if user:
        return redirect("/")
    return render(request, "auth/login.html", {"active": "login", "next": next or ""})


@router.post("/login")
def login(
    request: Request,
    db: DbDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "",
):
    email = email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if not user or not user.password_hash or not verify_password(password, user.password_hash):
        return render(request, "auth/login.html",
                      {"active": "login", "error": "Invalid email or password.",
                       "email": email, "next": next}, status_code=401)
    if user.deleted_at is not None or user.status == UserStatus.suspended:
        return render(request, "auth/login.html",
                      {"active": "login", "error": "This account is not active.",
                       "email": email, "next": next}, status_code=403)
    if user.status == UserStatus.pending_verify:
        return render(request, "auth/login.html",
                      {"active": "login", "error": "Verify your email before signing in.",
                       "email": email, "next": next}, status_code=403)
    user.last_login_at = utcnow()
    session_id = new_uuid()
    activate_session(db, user, session_id)
    db.commit()

    # Only local pages in this web app may be used as post-login destinations.
    # A value such as //example.com is an external browser redirect.
    dest = next if next and (next == "/app" or next.startswith("/app/")) \
        and "\\" not in next else (
        "/recruiter" if user.role.value in ("recruiter", "employer", "admin") else "/dashboard")
    resp = redirect(dest, flash="Signed in.")
    set_session(resp, create_web_session_token(user.user_id, user.role.value, session_id=session_id))
    return resp


@router.get("/logout")
def logout(request: Request):
    resp = redirect("/", flash="Signed out.")
    clear_session(resp)
    return resp
