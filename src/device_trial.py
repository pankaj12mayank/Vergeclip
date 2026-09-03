"""
src/device_trial.py
-------------------
Device-based 1 trial per system (as per plan: har system pe 1 trial hi).

- Device fingerprint from frontend (hash of UA + screen + etc) sent as X-Device-Id header or body.device_id
- Stored in device_trials table, max_trials=1
- Guest and logged-in both checked (unless admin)
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.models import DeviceTrial, SessionLocal


def get_or_create_device(device_id: str, ip: str | None = None, db: Session | None = None) -> DeviceTrial:
    own = False
    if db is None:
        db = SessionLocal()
        own = True
    try:
        dev = db.query(DeviceTrial).filter(DeviceTrial.device_id == device_id).first()
        if not dev:
            dev = DeviceTrial(device_id=device_id, ip_address=ip, trials_used=0, max_trials=1, is_blocked=False)
            db.add(dev)
            db.commit()
            db.refresh(dev)
        else:
            dev.last_seen = datetime.now(timezone.utc)
            if ip and not dev.ip_address:
                dev.ip_address = ip
            db.commit()
        # Heal legacy auto-blocks (see check_device_trial for rationale)
        if dev.is_blocked and dev.trials_used >= 1:
            dev.is_blocked = False
            db.commit()
        db.refresh(dev)
        return dev
    finally:
        if own:
            db.close()


def _dev_dict(dev: DeviceTrial) -> dict:
    """Serialize DeviceTrial to dict (call while session is still open)."""
    return {
        "device_id": dev.device_id,
        "ip_address": dev.ip_address,
        "trials_used": dev.trials_used,
        "max_trials": dev.max_trials,
        "is_blocked": dev.is_blocked,
        "first_seen": dev.first_seen.isoformat() if dev.first_seen else None,
        "last_seen": dev.last_seen.isoformat() if dev.last_seen else None,
    }


def check_device_trial(device_id: str, ip: str | None = None, db: Session | None = None) -> dict:
    """Return {allowed: bool, reason, trials_used, max_trials}"""
    if not device_id or len(device_id) < 8:
        return {"allowed": True, "reason": "no device id", "trials_used": 0, "max_trials": 1}
    own = False
    if db is None:
        db = SessionLocal()
        own = True
    try:
        dev = db.query(DeviceTrial).filter(DeviceTrial.device_id == device_id).first()
        if not dev:
            dev = DeviceTrial(device_id=device_id, ip_address=ip, trials_used=0, max_trials=1, is_blocked=False)
            db.add(dev)
            db.commit()
            db.refresh(dev)
        else:
            dev.last_seen = datetime.now(timezone.utc)
            if ip and not dev.ip_address:
                dev.ip_address = ip
            db.commit()
        # Heal legacy rows: the old code auto-"blocked" every device after its
        # single trial, which falsely read as "blocked by admin" forever. There is
        # no manual block feature, so any such block was automatic — clear it. The
        # exhausted trial itself still counts via trials_used.
        if dev.is_blocked and dev.trials_used >= 1:
            dev.is_blocked = False
            dev.last_seen = datetime.now(timezone.utc)
            db.commit()
        # Read all attributes into locals before session closes
        is_blocked = dev.is_blocked
        trials_used = dev.trials_used
        max_trials = dev.max_trials
        if is_blocked:
            return {"allowed": False, "reason": "Device blocked by admin", "trials_used": trials_used, "max_trials": max_trials}
        if trials_used >= max_trials:
            return {"allowed": False, "reason": f"Trial already used on this device ({trials_used}/{max_trials}). Contact admin to reset.", "trials_used": trials_used, "max_trials": max_trials}
        return {"allowed": True, "reason": "Trial available", "trials_used": trials_used, "max_trials": max_trials}
    finally:
        if own:
            db.close()


def consume_device_trial(device_id: str, ip: str | None = None, db: Session | None = None) -> dict:
    """Increment trials_used. Raise 403 if already max. Returns dict (session-safe)."""
    if not device_id or len(device_id) < 8:
        d = get_or_create_device(device_id or "unknown", ip, db)
        return _dev_dict(d)
    own = False
    if db is None:
        db = SessionLocal()
        own = True
    try:
        dev = db.query(DeviceTrial).filter(DeviceTrial.device_id == device_id).first()
        if not dev:
            dev = DeviceTrial(device_id=device_id, ip_address=ip, trials_used=0, max_trials=1, is_blocked=False)
            db.add(dev)
            db.flush()
        trials_used = dev.trials_used
        max_trials = dev.max_trials
        is_blocked = dev.is_blocked
        if dev.trials_used >= max_trials or dev.is_blocked:
            raise HTTPException(status_code=403, detail=f"Trial already used on this device ({trials_used}/{max_trials}). No more trials. Contact admin.")
        dev.trials_used += 1
        dev.last_seen = datetime.now(timezone.utc)
        # NOTE: no auto-block here. An exhausted trial must NOT permanently lock a
        # device behind a fake "blocked by admin" banner — only an explicit admin
        # action (none exists today) should set is_blocked.
        db.commit()
        db.refresh(dev)
        # Read attributes into locals before session closes
        return _dev_dict(dev)
    finally:
        if own:
            db.close()


def reset_device_trial(device_id: str, db: Session | None = None) -> dict:
    own = False
    if db is None:
        db = SessionLocal()
        own = True
    try:
        dev = db.query(DeviceTrial).filter(DeviceTrial.device_id == device_id).first()
        if not dev:
            raise HTTPException(status_code=404, detail="Device not found")
        dev.trials_used = 0
        dev.is_blocked = False
        dev.last_seen = datetime.now(timezone.utc)
        db.commit()
        db.refresh(dev)
        return _dev_dict(dev)
    finally:
        if own:
            db.close()
