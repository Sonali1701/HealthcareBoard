"""Transactional email via SendGrid.

When ``settings.email_enabled`` is False (local dev), emails are logged to the
console instead of sent. Every send is best-effort: a failure is logged and
returns False but never raises, so a slow or misconfigured mail provider can
never break the request that triggered it.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from html import escape

from ..config import settings

logger = logging.getLogger("healthboard.email")


def send_email(to: str, subject: str, html: str, reply_to: str | None = None) -> bool:
    """Send an email. Returns True if dispatched to SendGrid, False otherwise.

    reply_to routes replies somewhere other than the from-address — used so a
    candidate's reply to a recruiter's outreach lands in the recruiter's inbox.
    """
    if not to:
        return False
    if not settings.email_enabled or not settings.sendgrid_api_key:
        logger.info("EMAIL (not sent — email disabled)\n  to=%s\n  subject=%s", to, subject)
        return False
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Content, Email, Mail, ReplyTo, To

        message = Mail(
            from_email=Email(settings.email_from, settings.email_from_name),
            to_emails=To(to),
            subject=subject,
            html_content=Content("text/html", html),
        )
        if reply_to:
            message.reply_to = ReplyTo(reply_to)
        client = SendGridAPIClient(settings.sendgrid_api_key)
        resp = client.send(message)
        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.error("SendGrid returned %s for %s", resp.status_code, to)
        return ok
    except Exception:  # noqa: BLE001 — never let email failure break the request
        logger.exception("Failed to send email to %s", to)
        return False


def _base() -> str:
    return settings.frontend_base_url.rstrip("/")


def _wrap(heading: str, body_html: str, cta_text: str = "", cta_link: str = "") -> str:
    """One branded shell so every email looks like it came from the same place."""
    cta = (
        f'<a href="{cta_link}" style="display:inline-block;background:#075fe8;color:#fff;'
        f'text-decoration:none;padding:11px 22px;border-radius:8px;font-weight:600;'
        f'margin:8px 0 4px">{escape(cta_text)}</a>'
        if cta_text and cta_link else ""
    )
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'max-width:520px;margin:0 auto;color:#0f2f66">'
        '<div style="font-size:19px;font-weight:700;color:#075fe8;padding:8px 0 16px">HealthBoard</div>'
        f'<h1 style="font-size:19px;margin:0 0 12px;color:#082452">{escape(heading)}</h1>'
        f'<div style="font-size:14px;line-height:1.6;color:#405f8d">{body_html}</div>'
        f'{cta}'
        '<hr style="border:0;border-top:1px solid #dce7f7;margin:22px 0 12px">'
        '<div style="font-size:11px;color:#96add0">'
        'HealthBoard — a screened directory of healthcare professionals.<br>'
        'You are receiving this because you have a HealthBoard account.'
        '</div></div>'
    )


# --- Recruiter outreach ---------------------------------------------------

def send_recruiter_message(to: str, *, candidate_name: str, from_label: str,
                           reply_to: str, subject: str, message: str) -> bool:
    """A recruiter's message to an off-platform candidate, delivered by email.

    The candidate has no HealthBoard account, so their reply is routed straight
    to the recruiter's own inbox via reply_to rather than back into the app.
    """
    safe = escape(message).replace("\n", "<br>")
    greeting = f"Hi {escape(candidate_name)}," if candidate_name else "Hello,"
    html = _wrap(
        f"A message from {from_label}",
        f"<p>{greeting}</p><p>{safe}</p>"
        f"<p style='margin-top:16px;color:#607da8'>— {escape(from_label)}<br>"
        f"<span style='font-size:12px'>Reply to this email to respond directly.</span></p>",
    )
    return send_email(to, subject, html, reply_to=reply_to)


def send_credential_expiry(email: str, *, name: str, credential: str,
                           expiry_date, days_left: int) -> bool:
    """Remind a clinician that a licence or certification is about to lapse."""
    when = ("has expired" if days_left <= 0
            else f"expires in {days_left} day{'s' if days_left != 1 else ''}")
    html = _wrap(
        "A credential needs renewing",
        f"<p>Hi {escape(name or 'there')},</p>"
        f"<p>Your <strong>{escape(credential)}</strong> {when} "
        f"(expiry {escape(str(expiry_date))}). Renewing before it lapses keeps you "
        f"placeable — an expired credential can pause or lose an assignment.</p>",
        "Update your credentials", f"{_base()}/?page=credentials",
    )
    return send_email(email, f"Reminder: your {credential} {when}", html)


def send_notification_email(to: str, *, title: str, body: str,
                            cta_text: str = "Open HealthBoard",
                            cta_link: str | None = None) -> bool:
    """A branded email mirroring an in-app notification (new message, offer…)."""
    html = _wrap(
        title,
        f"<p>{escape(body)}</p>",
        cta_text, cta_link or _base(),
    )
    return send_email(to, title, html)


# --- Account emails -------------------------------------------------------

def send_password_reset(email: str, token: str) -> bool:
    link = f"{_base()}/reset-password?token={token}"
    html = _wrap(
        "Reset your password",
        "<p>We received a request to reset your HealthBoard password. This link "
        "expires in one hour.</p>"
        "<p>If you didn't request this, you can safely ignore this email — your "
        "password will not change.</p>",
        "Reset your password", link,
    )
    return send_email(email, "Reset your HealthBoard password", html)


def send_email_verification(email: str, token: str) -> bool:
    link = f"{_base()}/verify-email?token={token}"
    html = _wrap(
        "Confirm your email",
        "<p>Welcome to HealthBoard. Confirm this is your email address to "
        "activate your account.</p>",
        "Verify your email", link,
    )
    return send_email(email, "Verify your HealthBoard email", html)


def send_medhunt_login_code(email: str, code: str, expires_minutes: int = 10) -> bool:
    """Send the short-lived passwordless code used by the Medhunt extension."""
    html = _wrap(
        "Your Medhunt sign-in code",
        f"<p>Enter this code in the Medhunt browser extension:</p>"
        f"<p style='font-size:28px;letter-spacing:8px;font-weight:700;color:#075fe8'>"
        f"{escape(code)}</p>"
        f"<p>The code expires in {int(expires_minutes)} minutes. If you did not "
        "request it, you can safely ignore this email.</p>",
    )
    return send_email(email, f"{code} is your Medhunt sign-in code", html)


# --- Activity emails (sent alongside the in-app notification) --------------

def send_new_application(email: str, candidate_name: str, job_title: str) -> bool:
    html = _wrap(
        "New application",
        f"<p><strong>{escape(candidate_name)}</strong> just applied to your role "
        f"<strong>{escape(job_title)}</strong>.</p>"
        "<p>Open your portal to review their details and move them through your "
        "pipeline.</p>",
        "Review applicants", f"{_base()}/?page=employer",
    )
    return send_email(email, f"New application: {job_title}", html)


def send_application_update(email: str, job_title: str, status: str) -> bool:
    html = _wrap(
        "Your application moved forward",
        f"<p>Your application for <strong>{escape(job_title)}</strong> is now "
        f"<strong>{escape(status)}</strong>.</p>",
        "Track your applications", f"{_base()}/?page=applications",
    )
    return send_email(email, f"Update on your application: {job_title}", html)


def send_team_invite(email: str, org_name: str, accept_link: str | None = None) -> bool:
    if accept_link:
        html = _wrap(
            f"Join {escape(org_name)} on HealthBoard",
            f"<p>You've been invited to join <strong>{escape(org_name)}</strong> on "
            "HealthBoard — its shared talent pools, submissions and jobs.</p>"
            "<p>Accept the invitation to get started. If you don't have an account "
            "yet, you'll be able to create one first. This link expires in 14 days.</p>",
            "Accept invitation", accept_link,
        )
        return send_email(email, f"You're invited to join {org_name} on HealthBoard", html)
    html = _wrap(
        "You've been added to a team",
        f"<p>You now have access to <strong>{escape(org_name)}</strong> on "
        "HealthBoard — its shared talent pools, submissions and jobs.</p>",
        "Open HealthBoard", f"{_base()}/?page=employer",
    )
    return send_email(email, f"You've been added to {org_name}", html)


def send_weekly_usage_report(email: str, *, org_name: str, period_start,
                             period_end, members: list[dict], totals: dict) -> bool:
    """Send an owner/admin the organization's completed weekly credit report."""
    last_day = period_end - timedelta(days=1)
    period_label = f"{period_start:%b %d}-{last_day:%b %d, %Y}"
    rows = "".join(
        "<tr>"
        f"<td style='padding:8px;border-bottom:1px solid #e5edf8'>"
        f"<strong>{escape(m.get('name') or m.get('email') or 'Team member')}</strong><br>"
        f"<span style='font-size:12px;color:#7189ad'>{escape(m.get('email') or '')}</span></td>"
        f"<td style='padding:8px;border-bottom:1px solid #e5edf8'>{escape(m.get('role_label') or '')}</td>"
        f"<td style='padding:8px;text-align:right;border-bottom:1px solid #e5edf8'>"
        f"<strong>{int(m.get('credits_used') or 0):,}</strong></td>"
        f"<td style='padding:8px;text-align:right;border-bottom:1px solid #e5edf8'>"
        f"{int(m.get('contacts_revealed') or 0):,}</td>"
        f"<td style='padding:8px;text-align:right;border-bottom:1px solid #e5edf8'>"
        f"{int(m.get('credits_remaining') or 0):,}</td>"
        "</tr>"
        for m in members
    )
    body = (
        f"<p>Here is the credit usage for <strong>{escape(org_name)}</strong> "
        f"for {escape(period_label)}.</p>"
        "<div style='display:flex;gap:18px;margin:16px 0'>"
        f"<div><strong style='font-size:20px'>{int(totals.get('credits_used') or 0):,}</strong>"
        "<br><span style='font-size:12px'>credits used</span></div>"
        f"<div><strong style='font-size:20px'>{int(totals.get('contacts_revealed') or 0):,}</strong>"
        "<br><span style='font-size:12px'>contacts revealed</span></div>"
        f"<div><strong style='font-size:20px'>{int(totals.get('credits_remaining') or 0):,}</strong>"
        "<br><span style='font-size:12px'>credits remaining</span></div></div>"
        "<div style='overflow-x:auto'><table style='border-collapse:collapse;width:100%;font-size:13px'>"
        "<thead><tr style='background:#f3f7fd'>"
        "<th style='padding:8px;text-align:left'>Member</th>"
        "<th style='padding:8px;text-align:left'>Role</th>"
        "<th style='padding:8px;text-align:right'>Used</th>"
        "<th style='padding:8px;text-align:right'>Reveals</th>"
        "<th style='padding:8px;text-align:right'>Left</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
    )
    html = _wrap(
        "Weekly organization credit usage", body,
        "View organization usage", f"{_base()}/?page=orgadmin",
    )
    return send_email(
        email, f"Weekly credit usage - {org_name} - {period_label}", html
    )
