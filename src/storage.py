"""
src/storage.py
--------------
Per-user storage (Phase F) — local disk, no S3 yet, per-user isolation.

- get_user_storage_path(user_id) -> ./storage/{user_id}/
- save_clip(user_id, job_id, file_bytes, filename) -> path
- get_clip_url(user_id, job_id, filename) -> /files/{user_id}/{job_id}/{filename}
- delete_old_clips(days=30) cleanup
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import PROJECT_ROOT, get_setting

def _resolve_storage_root() -> Path:
    """Resolve storage root from DB settings (dynamic)."""
    try:
        sp = get_setting("STORAGE_PATH", "./storage")
    except Exception:
        sp = "./storage"
    if not os.path.isabs(sp):
        return (PROJECT_ROOT / sp).resolve()
    return Path(sp)

STORAGE_ROOT = _resolve_storage_root()
STORAGE_ROOT.mkdir(parents=True, exist_ok=True)


def get_user_storage_path(user_id: int | str) -> Path:
    p = STORAGE_ROOT / str(user_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_job_storage_path(user_id: int | str, job_id: str) -> Path:
    p = get_user_storage_path(user_id) / str(job_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_clip(user_id: int | str, job_id: str, filename: str, file_bytes: bytes | None = None, src_path: Path | None = None) -> Path:
    """Save clip either from bytes or copy from src_path."""
    # Sanitize filename
    safe_name = Path(filename).name
    if not safe_name or safe_name in (".", ".."):
        raise ValueError("Invalid filename")
    dest = get_job_storage_path(user_id, job_id) / safe_name
    if file_bytes is not None:
        dest.write_bytes(file_bytes)
    elif src_path and src_path.exists():
        shutil.copy2(str(src_path), str(dest))
    else:
        raise ValueError("Need file_bytes or src_path")
    return dest


def get_clip_url(user_id: int | str, job_id: str, filename: str) -> str:
    return f"/files/{user_id}/{job_id}/{Path(filename).name}"


def delete_old_clips(days: int = 30) -> int:
    """Delete clips older than days, return count deleted."""
    cutoff = time.time() - days * 86400
    deleted = 0
    for user_dir in STORAGE_ROOT.iterdir():
        if not user_dir.is_dir():
            continue
        for job_dir in user_dir.iterdir():
            if not job_dir.is_dir():
                continue
            for f in job_dir.glob("*"):
                try:
                    if f.stat().st_mtime < cutoff:
                        if f.is_file():
                            f.unlink(missing_ok=True)
                            deleted += 1
                        elif f.is_dir():
                            shutil.rmtree(f, ignore_errors=True)
                            deleted += 1
                except Exception:
                    continue
            # Remove empty job dir
            try:
                if not any(job_dir.iterdir()):
                    job_dir.rmdir()
            except Exception:
                pass
    return deleted
