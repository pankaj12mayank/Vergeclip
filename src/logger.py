"""
logger.py
---------
Configures a single application-wide logger that writes to both the
console (with colour) and a rotating log file under logs/.

Usage:
    from src.logger import get_logger
    log = get_logger(__name__)
    log.info("Hello from %s", __name__)
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init(autoreset=True)
except ImportError:
    class _ColorFallback:
        def __getattr__(self, name):
            return ""
    Fore = _ColorFallback()
    Style = _ColorFallback()

from src.config import LOGS_DIR


import io

# Ensure UTF-8 output on Windows streams
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


class _ColourFormatter(logging.Formatter):
    """Attach ANSI colour codes to log-level labels for console output."""

    _COLOURS = {
        logging.DEBUG:    Fore.CYAN,
        logging.INFO:     Fore.GREEN,
        logging.WARNING:  Fore.YELLOW,
        logging.ERROR:    Fore.RED,
        logging.CRITICAL: Fore.MAGENTA,
    }
    _FMT = "[%(asctime)s] %(levelname)-8s %(name)s - %(message)s"
    _DATE = "%H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:  # noqa: D102
        colour = self._COLOURS.get(record.levelno, "")
        # Format message safely
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        
        # Replace non-ascii if needed for standard console
        record.message = msg
        formatter = logging.Formatter(
            f"{colour}{self._FMT}{Style.RESET_ALL}", datefmt=self._DATE
        )
        return formatter.format(record)


def get_logger(name: str, level: int = logging.DEBUG) -> logging.Logger:
    """
    Return a named logger with console + file handlers attached.

    Calling this multiple times with the same *name* returns the same
    logger (Python's logging registry de-duplicates handlers).
    """
    logger = logging.getLogger(name)

    if logger.handlers:          # already configured — skip re-init
        return logger

    logger.setLevel(level)

    # ── Console handler (coloured) ──────────────────────────────────────────
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(_ColourFormatter())
    logger.addHandler(console_handler)

    # ── Rotating file handler (plain text) ──────────────────────────────────
    log_file = LOGS_DIR / "podcast_shorts.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,   # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(levelname)-8s %(name)s — %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(file_handler)

    return logger


# ── Real-Time System & User Audit Event Buffer ────────────────────────────────
from datetime import datetime, timezone
import collections
import uuid

_MAX_AUDIT_LOGS = 1000
SYSTEM_EVENT_LOGS = collections.deque(maxlen=_MAX_AUDIT_LOGS)

def log_system_event(
    category: str,
    action: str,
    detail: str,
    user_id: str | int | None = None,
    severity: str = "INFO",
    ip: str | None = None,
    request_data: dict | str | None = None,
    response_data: dict | str | None = None
) -> dict:
    """
    Log a real-time event into the audit ring-buffer, DB, and main logger with payload tracking.
    Categories: "AUTH", "PIPELINE", "CONFIG", "AI_PROVIDER", "QUOTA", "ERROR", "SYSTEM"
    Severities: "INFO", "SUCCESS", "WARN", "ERROR"
    """
    event_id = str(uuid.uuid4())[:8]
    event = {
        "id": event_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "category": str(category).upper(),
        "action": action,
        "detail": detail,
        "user_id": str(user_id) if user_id is not None else "System",
        "severity": str(severity).upper(),
        "ip": ip or "127.0.0.1",
        "request_data": request_data,
        "response_data": response_data
    }
    SYSTEM_EVENT_LOGS.appendleft(event)
    
    # Persist to database
    try:
        from src.models import AuditLog, SessionLocal
        import json as _json
        db = SessionLocal()
        try:
            log_entry = AuditLog(
                event_id=event_id,
                category=event["category"],
                action=action,
                detail=detail,
                user_id=event["user_id"],
                severity=event["severity"],
                ip=event["ip"],
                request_data=_json.dumps(request_data) if request_data else None,
                response_data=_json.dumps(response_data) if response_data else None,
            )
            db.add(log_entry)
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
    except Exception:
        pass
    
    # Also log to root logger
    _log = get_logger("audit")
    if severity.upper() == "ERROR":
        _log.error("[%s] %s: %s (User: %s)", category, action, detail, user_id)
    elif severity.upper() == "WARN":
        _log.warning("[%s] %s: %s (User: %s)", category, action, detail, user_id)
    else:
        _log.info("[%s] %s: %s (User: %s)", category, action, detail, user_id)
        
    return event

def delete_batch_audit_events(event_ids: list[str]) -> int:
    """Delete audit events by event_id from both DB and in-memory deque."""
    id_set = set(event_ids)
    # Remove from deque
    global SYSTEM_EVENT_LOGS
    new_deque = collections.deque(
        (ev for ev in SYSTEM_EVENT_LOGS if ev.get("id") not in id_set),
        maxlen=_MAX_AUDIT_LOGS
    )
    removed = len(SYSTEM_EVENT_LOGS) - len(new_deque)
    SYSTEM_EVENT_LOGS = new_deque
    # Remove from DB
    try:
        from src.models import AuditLog, SessionLocal
        db = SessionLocal()
        try:
            db.query(AuditLog).filter(AuditLog.event_id.in_(id_set)).delete(synchronize_session=False)
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
    except Exception:
        pass
    return removed

def clear_all_audit_events() -> int:
    """Purge all audit events from both DB and in-memory deque."""
    count = len(SYSTEM_EVENT_LOGS)
    SYSTEM_EVENT_LOGS.clear()
    # Clear DB
    try:
        from src.models import AuditLog, SessionLocal
        db = SessionLocal()
        try:
            count = db.query(AuditLog).count()
            db.query(AuditLog).delete()
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
    except Exception:
        pass
    return count
