"""
src/quota.py
------------
Usage quota (Phase C) — no payment, DB-tracked monthly limits.

FREE_TIER_MONTHLY_LIMIT from config (default 5)
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.config import PROJECT_ROOT
from src.models import UsageQuota, SessionLocal

def _month_year_now() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def check_and_increment_quota(user_id: int, db: Session, limit: int | None = None) -> int:
    """
    Check quota for current month, increment if allowed.
    Admin / System Owner is completely exempt (Unlimited ∞).
    Returns remaining count.
    Raises 429 if limit reached for regular users.
    """
    from src.models import User
    u = db.query(User).filter(User.id == user_id).first()
    if u and (u.role == "admin" or u.id == 1 or u.username == "admin"):
        # System Owner has full unlimited access
        return 999999

    # Get limit from settings if not passed
    if limit is None:
        try:
            from src.config import get_setting
            limit = int(get_setting("FREE_TIER_MONTHLY_LIMIT", "5"))
        except Exception:
            limit = 5
    month = _month_year_now()
    quota = db.query(UsageQuota).filter(UsageQuota.user_id == user_id, UsageQuota.month_year == month).first()
    if not quota:
        quota = UsageQuota(user_id=user_id, month_year=month, videos_processed=0, videos_limit=limit)
        db.add(quota)
        db.flush()
    # Ensure limit is up to date (admin may change)
    quota.videos_limit = limit
    if quota.videos_processed >= quota.videos_limit:
        raise HTTPException(status_code=429, detail=f"Monthly limit reached ({quota.videos_limit} videos/month). Try next month.")
    quota.videos_processed += 1
    db.commit()
    remaining = quota.videos_limit - quota.videos_processed
    return remaining


def get_quota_remaining(user_id: int, db: Session) -> dict:
    from src.models import User
    u = db.query(User).filter(User.id == user_id).first()
    is_admin = bool(u and (u.role == "admin" or u.id == 1 or u.username == "admin"))

    month = _month_year_now()
    quota = db.query(UsageQuota).filter(UsageQuota.user_id == user_id, UsageQuota.month_year == month).first()
    processed = quota.videos_processed if quota else 0

    if is_admin:
        return {
            "month_year": month,
            "videos_processed": processed,
            "videos_limit": "Unlimited (System Owner)",
            "remaining": "Unlimited ∞",
            "is_owner": True
        }

    if not quota:
        try:
            from src.config import get_setting
            limit = int(get_setting("FREE_TIER_MONTHLY_LIMIT", "5"))
        except Exception:
            limit = 5
        return {"month_year": month, "videos_processed": 0, "videos_limit": limit, "remaining": limit, "is_owner": False}
    return {
        "month_year": quota.month_year,
        "videos_processed": quota.videos_processed,
        "videos_limit": quota.videos_limit,
        "remaining": max(0, quota.videos_limit - quota.videos_processed),
        "is_owner": False
    }


def refund_quota(user_id: int, db: Session):
    """Refund one quota if job failed before processing (call on failure)."""
    month = _month_year_now()
    quota = db.query(UsageQuota).filter(UsageQuota.user_id == user_id, UsageQuota.month_year == month).first()
    if quota and quota.videos_processed > 0:
        quota.videos_processed = max(0, quota.videos_processed - 1)
        db.commit()
