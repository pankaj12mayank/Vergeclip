"""
src.smtp_service.py
-------------------
Minimal SMTP email service for password resets and notifications.
Stores config in the Setting DB table.
"""

from __future__ import annotations

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional

from src.config import get_setting, set_setting
from src.logger import get_logger

log = get_logger(__name__)


def get_smtp_config() -> dict:
    """Load current SMTP config from DB."""
    return {
        "host": get_setting("smtp_host", ""),
        "port": int(get_setting("smtp_port", "587")),
        "username": get_setting("smtp_username", ""),
        "password": get_setting("smtp_password", ""),
        "sender_email": get_setting("smtp_sender_email", ""),
        "sender_name": get_setting("smtp_sender_name", "Vergeclip AI Security"),
        "use_tls": get_setting("smtp_use_tls", "true") == "true",
    }


def save_smtp_config(
    host: str,
    port: int,
    username: str,
    password: str,
    sender_email: str,
    sender_name: str = "Vergeclip AI Security",
    use_tls: bool = True,
) -> None:
    """Save SMTP config to DB."""
    set_setting("smtp_host", host)
    set_setting("smtp_port", str(port))
    set_setting("smtp_username", username)
    if password:
        # Only overwrite when a new password was entered; blank keeps existing
        set_setting("smtp_password", password)
    set_setting("smtp_sender_email", sender_email)
    set_setting("smtp_sender_name", sender_name)
    set_setting("smtp_use_tls", "true" if use_tls else "false")
    log.info("SMTP config saved: %s:%d", host, port)


def load_smtp_config() -> dict:
    """Alias for get_smtp_config."""
    return get_smtp_config()


def send_smtp_email(
    to_email: str,
    subject: str,
    body: str,
    html: bool = False,
    config: Optional[dict] = None,
) -> bool:
    """Send an email via configured SMTP. Returns True on success."""
    cfg = config or get_smtp_config()
    if not cfg.get("host") or not cfg.get("sender_email"):
        log.warning("SMTP not configured — cannot send email to %s", to_email)
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{cfg.get('sender_name', 'Vergeclip')} <{cfg['sender_email']}>"
        msg["To"] = to_email
        msg["Subject"] = subject

        if html:
            msg.attach(MIMEText(body, "html", "utf-8"))
        else:
            msg.attach(MIMEText(body, "plain", "utf-8"))

        port = int(cfg.get("port", 587))
        use_tls = cfg.get("use_tls", True)

        with smtplib.SMTP(cfg["host"], port, timeout=15) as server:
            if use_tls:
                server.starttls()
            if cfg.get("username") and cfg.get("password"):
                server.login(cfg["username"], cfg["password"])
            server.sendmail(cfg["sender_email"], [to_email], msg.as_string())

        log.info("SMTP email sent to %s: %s", to_email, subject)
        return True

    except Exception as e:
        log.error("SMTP send failed to %s: %s", to_email, e)
        return False


# ── Password Reset Token Management ───────────────────────────────────────────

import secrets
import time

_reset_tokens: dict[str, dict] = {}  # token -> {"user_id": int, "expires": float}


def create_password_reset_token(user_id: int, expiry_secs: int = 3600) -> str:
    """Generate a time-limited password reset token."""
    token = secrets.token_urlsafe(32)
    _reset_tokens[token] = {
        "user_id": user_id,
        "expires": time.time() + expiry_secs,
    }
    return token


def verify_and_consume_reset_token(token: str) -> Optional[int]:
    """Verify a reset token and return user_id if valid. Consumes the token."""
    if not token or token not in _reset_tokens:
        return None
    data = _reset_tokens.pop(token)
    if time.time() > data["expires"]:
        return None
    return data["user_id"]
