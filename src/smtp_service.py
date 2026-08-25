"""
src/smtp_service.py
-------------------
SMTP Email Service for Vergeclip AI:
- SMTP Configuration Storage & Verification
- Password Reset Token Generation & Email Dispatch
- Test Email Transmission
"""

from __future__ import annotations

import json
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime, timezone, timedelta
import secrets

from src.config import PROJECT_ROOT
from src.logger import get_logger, log_system_event

log = get_logger("smtp")

DATA_DIR = PROJECT_ROOT / "data"

SMTP_CONFIG_FILE = DATA_DIR / "smtp_config.json"
RESET_TOKENS_FILE = DATA_DIR / "reset_tokens.json"


def load_smtp_config() -> dict:
    """Load SMTP settings from data/smtp_config.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SMTP_CONFIG_FILE.exists():
        try:
            return json.loads(SMTP_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "host": "",
        "port": 587,
        "username": "",
        "password": "",
        "sender_email": "",
        "sender_name": "Vergeclip AI Security",
        "use_tls": True,
        "is_configured": False
    }


def get_smtp_config() -> dict:
    """Alias for load_smtp_config."""
    return load_smtp_config()


def save_smtp_config(host: str = "", port: int = 587, username: str = "", password: str = "", sender_email: str = "", sender_name: str = "Vergeclip AI Security", use_tls: bool = True, **kwargs):
    """Save SMTP settings to data/smtp_config.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    current = load_smtp_config()
    current.update({
        "host": str(host or kwargs.get("host") or current.get("host", "")).strip(),
        "port": int(port or kwargs.get("port") or current.get("port", 587)),
        "username": str(username or kwargs.get("username") or current.get("username", "")).strip(),
        "password": str(password or kwargs.get("password") or current.get("password", "")).strip(),
        "sender_email": str(sender_email or kwargs.get("sender_email") or current.get("sender_email", "")).strip(),
        "sender_name": str(sender_name or kwargs.get("sender_name") or current.get("sender_name", "Vergeclip AI Security")).strip(),
        "use_tls": bool(use_tls if use_tls is not None else kwargs.get("use_tls", True)),
        "is_configured": bool((host or current.get("host")) and (sender_email or current.get("sender_email")))
    })
    SMTP_CONFIG_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")
    log_system_event("CONFIG", "SMTP Settings Updated", f"Host: {current['host']}:{current['port']}, From: {current['sender_email']}", severity="SUCCESS")
    return current


def send_smtp_email(to_email: str, subject: str, html_content: str = "", text_content: Optional[str] = None, body_text: Optional[str] = None) -> Tuple[bool, str]:
    """Send an HTML/Text email using configured SMTP credentials."""
    cfg = load_smtp_config()
    host = cfg.get("host")
    port = int(cfg.get("port") or 587)
    username = cfg.get("username")
    password = cfg.get("password")
    sender_email = cfg.get("sender_email") or username
    sender_name = cfg.get("sender_name") or "Vergeclip AI"
    use_tls = cfg.get("use_tls", True)

    if not host or not sender_email:
        return False, "SMTP server is not configured in Admin Settings."

    if not html_content and body_text:
        html_content = f"<p>{body_text.replace(chr(10), '<br>')}</p>"
        text_content = body_text

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{sender_name} <{sender_email}>"
    msg["To"] = to_email

    if text_content:
        msg.attach(MIMEText(text_content, "plain", "utf-8"))
    if html_content:
        msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        if port == 465:
            # SSL
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context, timeout=15) as server:
                if username and password:
                    server.login(username, password)
                server.sendmail(sender_email, [to_email], msg.as_string())
        else:
            # TLS / STARTTLS
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.ehlo()
                if use_tls:
                    context = ssl.create_default_context()
                    server.starttls(context=context)
                    server.ehlo()
                if username and password:
                    server.login(username, password)
                server.sendmail(sender_email, [to_email], msg.as_string())

        log.info("Email sent successfully to %s: %s", to_email, subject)
        log_system_event("SYSTEM", "Email Sent", f"Subject: '{subject}' to {to_email}", severity="SUCCESS")
        return True, "Email sent successfully!"
    except Exception as e:
        log.error("SMTP send error to %s: %s", to_email, e)
        log_system_event("ERROR", "SMTP Send Failed", f"Failed to send email to {to_email}: {e}", severity="ERROR")
        return False, str(e)


# ── Password Reset Tokens Management ──────────────────────────────────────────
def _load_tokens() -> dict:
    if RESET_TOKENS_FILE.exists():
        try:
            return json.loads(RESET_TOKENS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_tokens(tokens: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RESET_TOKENS_FILE.write_text(json.dumps(tokens, indent=2), encoding="utf-8")


def create_password_reset_token(user_id: int, email: str) -> str:
    """Generate 32-character hex reset token expiring in 15 minutes."""
    tokens = _load_tokens()
    # Clean expired
    now = datetime.now(timezone.utc)
    active = {
        k: v for k, v in tokens.items()
        if datetime.fromisoformat(v["expires_at"]) > now
    }

    token = secrets.token_urlsafe(24)
    expires_at = (now + timedelta(minutes=15)).isoformat()
    active[token] = {
        "user_id": user_id,
        "email": email,
        "created_at": now.isoformat(),
        "expires_at": expires_at
    }
    _save_tokens(active)
    return token


def verify_and_consume_reset_token(token: str) -> Optional[int]:
    """Verify reset token and return user_id if valid."""
    tokens = _load_tokens()
    if token not in tokens:
        return None
    data = tokens[token]
    expires = datetime.fromisoformat(data["expires_at"])
    if datetime.now(timezone.utc) > expires:
        del tokens[token]
        _save_tokens(tokens)
        return None
    user_id = data["user_id"]
    del tokens[token]
    _save_tokens(tokens)
    return user_id
