"""
email_sender.py — Drop this into your project root.

Auto-detects whether SENDER_EMAIL is Outlook or Gmail
and uses the correct SMTP settings for each.

Usage in pipeline.py:
    from email_sender import send_digest_email
    send_digest_email(subject, html_body, recipient_list)
"""

import smtplib
import ssl
import os
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def _get_smtp_config(sender_email: str) -> dict:
    """Return correct SMTP host/port based on email domain."""
    domain = sender_email.split("@")[-1].lower()

    outlook_domains = {"outlook.com", "hotmail.com", "live.com", "office365.com"}
    # Catch corporate Microsoft 365 domains too
    # (most company emails route through office365 even with custom domain)

    if domain in outlook_domains:
        return {"host": "smtp.office365.com", "port": 587, "use_starttls": True}
    elif domain == "gmail.com":
        return {"host": "smtp.gmail.com", "port": 587, "use_starttls": True}
    else:
        # Corporate domain — almost certainly Microsoft 365
        # If not, override SMTP_HOST and SMTP_PORT in .env
        host = os.getenv("SMTP_HOST", "smtp.office365.com")
        port = int(os.getenv("SMTP_PORT", "587"))
        return {"host": host, "port": port, "use_starttls": True}


def send_digest_email(
    subject: str,
    html_body: str,
    recipients: list[str],
    sender_email: str | None = None,
    sender_password: str | None = None,
) -> bool:
    """
    Send the weekly digest email.

    Parameters
    ----------
    subject        : Email subject line
    html_body      : Full HTML string for the digest
    recipients     : List of recipient email addresses
    sender_email   : Override — defaults to SENDER_EMAIL env var
    sender_password: Override — defaults to SENDER_PASSWORD env var

    Returns True on success, False on failure (logs the error).
    """
    sender_email    = sender_email    or os.getenv("SENDER_EMAIL", "").strip()
    sender_password = sender_password or os.getenv("SENDER_PASSWORD", "").strip()

    if not sender_email or not sender_password:
        logger.error("SENDER_EMAIL or SENDER_PASSWORD not set in .env")
        return False

    if not recipients:
        logger.error("Recipient list is empty — check RECIPIENT_EMAILS in .env")
        return False

    cfg = _get_smtp_config(sender_email)
    logger.info(f"Sending via {cfg['host']}:{cfg['port']} as {sender_email}")

    # ── Build message ─────────────────────────────────────────────────────────
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender_email
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # ── Send ──────────────────────────────────────────────────────────────────
    try:
        context = ssl.create_default_context()

        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as server:
            server.ehlo()

            if cfg["use_starttls"]:
                server.starttls(context=context)
                server.ehlo()

            server.login(sender_email, sender_password)
            server.sendmail(sender_email, recipients, msg.as_bytes())

        logger.info(f"Digest sent to {len(recipients)} recipient(s)")
        return True

    except smtplib.SMTPAuthenticationError:
        logger.error(
            "SMTP authentication failed. "
            "For Outlook: ask IT to enable Authenticated SMTP (SMTP AUTH) for this account. "
            "For Gmail: use an App Password (not your regular password)."
        )
        return False

    except smtplib.SMTPException as e:
        logger.error(f"SMTP error: {e}")
        return False

    except TimeoutError:
        logger.error(
            f"Connection to {cfg['host']}:{cfg['port']} timed out. "
            "Corporate firewall may be blocking outbound SMTP. "
            "Try: add SMTP_HOST/SMTP_PORT overrides to .env, or test from outside the network."
        )
        return False

    except Exception as e:
        logger.error(f"Unexpected error sending email: {type(e).__name__}: {e}")
        return False


def parse_recipients(env_value: str) -> list[str]:
    """
    Parse RECIPIENT_EMAILS from .env.
    Handles comma-separated, semicolon-separated, and mixed whitespace.
    """
    if not env_value:
        return []
    import re
    parts = re.split(r"[,;\s]+", env_value.strip())
    return [p.strip() for p in parts if p.strip() and "@" in p]