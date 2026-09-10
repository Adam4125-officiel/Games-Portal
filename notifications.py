"""
notifications.py — best-effort outbound notifications: a Discord webhook and
email, inspired by status-portal's module of the same name (dropped: ntfy,
since nobody asked for it here - add it the same way if that changes).

Fire-and-forget: any failure (unreachable, misconfigured, timeout) is caught
and logged, never raised - a notification failing must never break the
request/status-update flow that triggered it.

Two audiences:
  * notify_admin() - the admin alert channel (Discord webhook + the
    admin_notify_email recipient list), for "a new request came in".
  * send_email() - a single arbitrary recipient, for a visitor whose Seerr
    contact info this app knows (see seerr.py) getting told their request's
    status changed. There's no per-visitor Discord delivery yet - that needs
    an actual bot (a persistent connection, not a one-way webhook), which is
    a bigger, optional piece of infrastructure status-portal itself keeps
    separate; see ROADMAP.md.
"""
import html
import logging
import smtplib
from email.message import EmailMessage

import requests

import config
import db

_logger = logging.getLogger(__name__)

TIMEOUT = 5

RECIPIENTS_SETTING = "admin_notify_email"


def admin_email_recipients():
    """The admin alert recipients. Comma-separated, blanks dropped.

    Stored as a DB setting rather than an env var: the host/from-address/
    credentials are deployment config, but *who gets told* is a routine
    choice an admin changes without editing a file and restarting - see
    CLAUDE.md's config split. PORTAL_SMTP_TO is the fallback for an install
    that set it before this existed."""
    try:
        raw = db.get_setting(RECIPIENTS_SETTING, "")
    except Exception:
        _logger.warning("Could not read the admin notification recipient list; "
                        "falling back to PORTAL_SMTP_TO")
        raw = ""
    if not raw.strip():
        raw = config.SMTP_TO
    return [address.strip() for address in raw.split(",") if address.strip()]


def normalize_recipients(raw):
    """Cleans admin-entered input (mixed commas/newlines/whitespace) into a
    canonical comma-separated string."""
    parts = [p.strip() for p in (raw or "").replace("\n", ",").split(",") if p.strip()]
    return ", ".join(parts)


def email_configured():
    """Email needs three things to work at all. A half-filled block counts as
    "not set up" rather than a channel that fails on every send."""
    return bool(config.SMTP_HOST and config.SMTP_FROM and admin_email_recipients())


def discord_configured():
    return bool(config.DISCORD_WEBHOOK_URL)


def channel_summary():
    return [
        {"key": "discord", "label": "Discord webhook",
         "description": "Posts to one Discord channel when a new request comes in.",
         "env_var": "PORTAL_DISCORD_WEBHOOK_URL", "configured": discord_configured()},
        {"key": "email", "label": "Email",
         "description": "Sent through your own SMTP server or provider. Needs a host, "
                        "a from-address and at least one recipient (set below).",
         "env_var": "PORTAL_SMTP_HOST", "configured": email_configured()},
    ]


def notify_admin(title, message):
    """Sends `title`/`message` to every configured admin channel. No-op if
    none are set up - never raises, so a call site never needs to guard it."""
    if discord_configured():
        _send_discord(title, message)
    if email_configured():
        send_email(title, message, recipients=admin_email_recipients())


def _send_discord(title, message):
    try:
        requests.post(config.DISCORD_WEBHOOK_URL,
                      json={"content": f"**{title}**\n{message}"}, timeout=TIMEOUT)
    except Exception as e:
        _logger.warning("Discord webhook failed: %s", e)


def build_email(subject, message, recipients):
    """A multipart/alternative message: plain text first, then a minimal HTML
    part (the *last* part in multipart/alternative is the preferred one).

    Built with html.escape() rather than a Jinja template - this module
    deliberately doesn't import Flask, since it can be called from a
    background thread with no request/app context to render one in, and a
    notification body is small enough that a template engine buys nothing."""
    email = EmailMessage()
    email["Subject"] = subject
    email["From"] = config.SMTP_FROM
    email["To"] = ", ".join(recipients)
    email.set_content(f"{subject}\n\n{message}\n\n-- \nSent by Games Portal.")
    email.add_alternative(f"""<html><body style="font-family: -apple-system, Segoe UI, Roboto, sans-serif;
   line-height: 1.5; color: #1a1d24;">
  <h2 style="margin: 0 0 12px; font-size: 18px;">{html.escape(subject)}</h2>
  <p style="margin: 0 0 16px; white-space: pre-wrap;">{html.escape(message)}</p>
  <p style="margin: 0; font-size: 12px; color: #6b7280;">Sent by Games Portal.</p>
</body></html>""", subtype="html")
    return email


def send_email(subject, message, recipients):
    """Sends one email to exactly the given recipients (no implicit default -
    callers with a specific person in mind, like a visitor's own Seerr-
    sourced address, must never accidentally fall back to the admin list).

    Best-effort like every other channel here: returns True/False rather than
    raising."""
    deliverable = [r for r in recipients if db.looks_like_email(r)]
    rejected = [r for r in recipients if not db.looks_like_email(r)]
    if rejected:
        _logger.warning("Not an email address, so not sent to: %s", ", ".join(repr(r) for r in rejected))
    recipients = deliverable
    if not (config.SMTP_HOST and config.SMTP_FROM and recipients):
        return False
    email = build_email(subject, message, recipients)
    try:
        with _smtp_connection() as smtp:
            if config.SMTP_USERNAME:
                smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            smtp.send_message(email)
        return True
    except Exception as e:
        # Including the recipients would put addresses in the log on every
        # failure; the count is enough to tell "one address is wrong" from
        # "SMTP is down".
        _logger.warning("Email to %d recipient(s) failed: %s", len(recipients), e)
        return False


def _smtp_connection():
    """'ssl' wraps the socket from the start (implicit TLS, usually port 465);
    'starttls' connects in the clear and upgrades (the common case, port
    587); anything else is unencrypted, sane only for a relay on the same
    machine or LAN. An unrecognised value is treated as starttls rather than
    silently downgrading to plaintext."""
    if config.SMTP_SECURITY == "ssl":
        return smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT,
                                timeout=config.SMTP_TIMEOUT_SECONDS)
    smtp = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=config.SMTP_TIMEOUT_SECONDS)
    if config.SMTP_SECURITY != "none":
        smtp.starttls()
    return smtp
