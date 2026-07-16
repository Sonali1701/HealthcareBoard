"""Transactional email via SendGrid.

When ``settings.email_enabled`` is False (local dev), emails are logged to the
console instead of sent, and auth endpoints surface the token in the response.
"""
from __future__ import annotations

import logging

from ..config import settings

logger = logging.getLogger("healthboard.email")


def send_email(to: str, subject: str, html: str) -> bool:
    """Send an email. Returns True if dispatched to SendGrid, False otherwise."""
    if not settings.email_enabled or not settings.sendgrid_api_key:
        logger.info("EMAIL (not sent — email disabled)\n  to=%s\n  subject=%s", to, subject)
        return False
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Content, Email, Mail, To

        message = Mail(
            from_email=Email(settings.email_from, settings.email_from_name),
            to_emails=To(to),
            subject=subject,
            html_content=Content("text/html", html),
        )
        client = SendGridAPIClient(settings.sendgrid_api_key)
        resp = client.send(message)
        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.error("SendGrid returned %s for %s", resp.status_code, to)
        return ok
    except Exception:  # noqa: BLE001 — never let email failure break the request
        logger.exception("Failed to send email to %s", to)
        return False


def send_password_reset(email: str, token: str) -> bool:
    link = f"{settings.frontend_base_url.rstrip('/')}/reset-password?token={token}"
    html = (
        f"<p>We received a request to reset your HealthBoard password.</p>"
        f"<p><a href=\"{link}\">Reset your password</a> (link expires in 1 hour).</p>"
        f"<p>If you didn't request this, you can ignore this email.</p>"
    )
    return send_email(email, "Reset your HealthBoard password", html)


def send_email_verification(email: str, token: str) -> bool:
    link = f"{settings.frontend_base_url.rstrip('/')}/verify-email?token={token}"
    html = (
        f"<p>Welcome to HealthBoard! Please confirm your email address.</p>"
        f"<p><a href=\"{link}\">Verify your email</a></p>"
    )
    return send_email(email, "Verify your HealthBoard email", html)
