"""Sending email over SMTP, for password-reset links.

Configured through MAIL_* settings (see config.py); Gmail needs a 16-character
"app password", not the account password. When nothing is configured, send_email
reports "unconfigured" rather than raising, so the caller can fall back to the
office-approval reset flow instead of erroring.
"""
import smtplib
from email.message import EmailMessage

from flask import current_app


def mail_configured():
    cfg = current_app.config
    return bool(cfg.get("MAIL_USERNAME") and cfg.get("MAIL_PASSWORD") and cfg.get("MAIL_FROM"))


def send_email(to_address, subject, body_text):
    """Returns {"status": "sent" | "unconfigured" | "failed", "detail": ...}.

    Never raises: a mail outage must not take down the page that calls it.
    """
    if not mail_configured():
        current_app.logger.info("[email] not configured - skipping send to %s", to_address)
        return {"status": "unconfigured", "detail": "Email is not configured."}

    cfg = current_app.config
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = cfg["MAIL_FROM"]
    message["To"] = to_address
    message.set_content(body_text)

    try:
        with smtplib.SMTP(cfg["MAIL_SERVER"], cfg["MAIL_PORT"], timeout=20) as server:
            if cfg.get("MAIL_USE_TLS", True):
                server.starttls()
            server.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
            server.send_message(message)
        return {"status": "sent", "detail": to_address}
    except Exception as exc:  # noqa: BLE001 - report any SMTP failure, don't crash
        current_app.logger.warning("[email] send failed: %s", exc)
        return {"status": "failed", "detail": str(exc)}
