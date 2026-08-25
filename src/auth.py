"""
src/auth.py
-----------
Fast, secure authentication module for Podcast Shorts Generator.

Features:
- SQLite user store (data/users.db) with thread-safe connections
- Bcrypt password hashing via passlib (constant-time verify)
- JWT (PyJWT) HS256 with configurable expiry
- FastAPI dependencies for protected routes
- Rate-limit friendly in-memory login attempt counter

Env:
  JWT_SECRET_KEY   -> secret for signing (default: dev-only fallback)
  JWT_ALGORITHM    -> default HS256
  JWT_EXPIRE_MIN   -> default 10080 (7 days)
  AUTH_REQUIRED    -> if "true" pipeline endpoints require token
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field

from src.config import PROJECT_ROOT
from src.logger import get_logger

log = get_logger("auth")

# ── Config ────────────────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get("JWT_SECRET_KEY", os.environ.get("SECRET_KEY", "")).strip()
if not SECRET_KEY:
    # Dev fallback - warn loudly
    SECRET_KEY = "dev-only-jwt-secret-change-in-production-please-set-JWT_SECRET_KEY"
    log.warning("JWT_SECRET_KEY not set - using insecure dev fallback. Set JWT_SECRET_KEY env var in production!")

ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256").strip() or "HS256"
EXPIRE_MINUTES = int(os.environ.get("JWT_EXPIRE_MIN", os.environ.get("JWT_EXPIRE_MINUTES", "10080")) or 10080)
AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "false").lower() in ("1", "true", "yes", "on")

# Bcrypt context - passlib handles salt/rounds
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)  # optional - we handle 401 manually

# SQLite location
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "users.db"


def _get_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL mode for concurrency + fast writes
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass
    return conn


def init_db() -> None:
    """Create users table if not exists. Idempotent."""
    conn = _get_conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                hashed_password TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                role TEXT NOT NULL DEFAULT 'user',
                tier TEXT NOT NULL DEFAULT 'free'
            );
            """
        )
        # Migrate columns if old DB schema
        try:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user';")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN tier TEXT NOT NULL DEFAULT 'free';")
        except Exception:
            pass

        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")
        conn.commit()

        # Ensure any user with username 'admin' has role 'admin'
        try:
            conn.execute("UPDATE users SET role='admin' WHERE username='admin' OR id=1;")
            conn.commit()
        except Exception:
            pass

        # ── Seed default user if empty (backend only) ──
        try:
            cur = conn.execute("SELECT COUNT(*) as cnt FROM users")
            cnt = cur.fetchone()["cnt"]
            if cnt == 0:
                default_username = os.environ.get("DEFAULT_ADMIN_USER", "admin").strip() or "admin"
                default_email = os.environ.get("DEFAULT_ADMIN_EMAIL", "admin@podcastshorts.ai").strip().lower() or "admin@podcastshorts.ai"
                default_password = os.environ.get("DEFAULT_ADMIN_PASSWORD", "Admin@123").strip() or "Admin@123"
                if len(default_password) < 8:
                    default_password = "Admin@123"
                hashed = pwd_context.hash(default_password)
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO users (username, email, hashed_password, created_at, is_active, role, tier) VALUES (?,?,?,?,1,'admin','pro')",
                    (default_username, default_email, hashed, now),
                )
                conn.commit()
                log.info("Seeded default admin user: %s (%s)", default_username, default_email)
        except Exception as seed_err:
            log.warning("Default user seed failed: %s", seed_err)
        log.info("Auth DB ready at %s", DB_PATH)
    finally:
        conn.close()


# Ensure DB on import
init_db()

# ── Pydantic Schemas ──────────────────────────────────────────────────────────
class SignupRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$")
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)

    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class LoginRequest(BaseModel):
    # Accept either username or email as identifier
    identifier: str = Field(..., min_length=3, max_length=254, description="username or email")
    password: str = Field(..., min_length=1, max_length=128)


class UserPublic(BaseModel):
    id: int
    username: str
    email: str
    created_at: str
    role: str = "user"
    tier: str = "free"


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserPublic


# ── Password helpers ──────────────────────────────────────────────────────────
def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:
        return False


# ── JWT helpers ───────────────────────────────────────────────────────────────
def create_access_token(data: dict, expires_minutes: Optional[int] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes or EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "iat": datetime.now(timezone.utc)})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc


# ── DB helpers ────────────────────────────────────────────────────────────────
def _row_to_public(row: sqlite3.Row) -> UserPublic:
    try:
        role = row["role"] if "role" in row.keys() else None
    except Exception:
        role = None

    username = str(row["username"])
    # Robust role determination: admin if explicitly 'admin', id=1, or username in admin set
    if not role or role == "user":
        if username.lower() in ("admin", "administrator", "root", "manager") or row["id"] == 1:
            role = "admin"
        else:
            role = "user"

    try:
        tier = row["tier"] if "tier" in row.keys() else "free"
    except Exception:
        tier = "free"

    return UserPublic(
        id=row["id"],
        username=username,
        email=row["email"],
        created_at=row["created_at"],
        role=role or "user",
        tier=tier or "free",
    )


def get_user_by_id(user_id: int) -> Optional[UserPublic]:
    conn = _get_conn()
    try:
        cur = conn.execute("SELECT * FROM users WHERE id=?", (user_id,))
        row = cur.fetchone()
        return _row_to_public(row) if row else None
    finally:
        conn.close()


def get_user_by_username_or_email(identifier: str) -> Optional[sqlite3.Row]:
    conn = _get_conn()
    try:
        cur = conn.execute("SELECT * FROM users WHERE username=? OR email=?", (identifier, identifier))
        return cur.fetchone()
    finally:
        conn.close()


def create_user(username: str, email: str, password: str) -> UserPublic:
    # Normalize
    username = username.strip()
    email = email.strip().lower()
    # Validate email/username uniqueness pre-check for nicer error
    conn = _get_conn()
    try:
        cur = conn.execute("SELECT id FROM users WHERE username=?", (username,))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="Username already taken")
        cur = conn.execute("SELECT id FROM users WHERE email=?", (email,))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="Email already registered")
        hashed = hash_password(password)
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO users (username, email, hashed_password, created_at, is_active) VALUES (?,?,?,?,1)",
            (username, email, hashed, now),
        )
        conn.commit()
        user_id = cur.lastrowid
        log.info("New user registered: %s (%s)", username, email)
        return UserPublic(id=user_id, username=username, email=email, created_at=now)
    except HTTPException:
        raise
    except sqlite3.IntegrityError as exc:
        # Race condition
        raise HTTPException(status_code=400, detail="Username or email already exists") from exc
    finally:
        conn.close()


def authenticate_user(identifier: str, password: str) -> Optional[sqlite3.Row]:
    row = get_user_by_username_or_email(identifier)
    if not row:
        return None
    if not row["is_active"]:
        return None
    if not verify_password(password, row["hashed_password"]):
        return None
    return row


def update_user_password(user_id: int, old_password: str, new_password: str) -> bool:
    conn = _get_conn()
    try:
        cur = conn.execute("SELECT * FROM users WHERE id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        if not verify_password(old_password, row["hashed_password"]):
            raise HTTPException(status_code=401, detail="Old password incorrect")
        if len(new_password) < 8:
            raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
        new_hash = hash_password(new_password)
        conn.execute("UPDATE users SET hashed_password=? WHERE id=?", (new_hash, user_id))
        conn.commit()
        log.info("Password changed for user_id %s", user_id)
        return True
    finally:
        conn.close()


def reset_user_password_by_id(user_id: int, new_password: str) -> bool:
    """Reset password directly without requiring old password (used by password reset tokens)."""
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    conn = _get_conn()
    try:
        cur = conn.execute("SELECT id FROM users WHERE id=?", (user_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="User not found")
        new_hash = hash_password(new_password)
        conn.execute("UPDATE users SET hashed_password=? WHERE id=?", (new_hash, user_id))
        conn.commit()
        log.info("Password reset successfully for user_id %s", user_id)
        return True
    finally:
        conn.close()


def update_user_profile(user_id: int, new_username: Optional[str] = None, new_email: Optional[str] = None) -> UserPublic:
    conn = _get_conn()
    try:
        cur = conn.execute("SELECT * FROM users WHERE id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        username = new_username.strip() if new_username else row["username"]
        email = new_email.strip().lower() if new_email else row["email"]
        if username != row["username"]:
            cur = conn.execute("SELECT id FROM users WHERE username=? AND id != ?", (username, user_id))
            if cur.fetchone():
                raise HTTPException(status_code=400, detail="Username already taken")
        if email != row["email"]:
            cur = conn.execute("SELECT id FROM users WHERE email=? AND id != ?", (email, user_id))
            if cur.fetchone():
                raise HTTPException(status_code=400, detail="Email already registered")
        conn.execute("UPDATE users SET username=?, email=? WHERE id=?", (username, email, user_id))
        conn.commit()
        return get_user_by_id(user_id)
    finally:
        conn.close()


# ── FastAPI Dependencies ──────────────────────────────────────────────────────
async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[UserPublic]:
    if not credentials or not credentials.credentials:
        return None
    payload = decode_token(credentials.credentials)
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    user = get_user_by_id(int(user_id))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> UserPublic:
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Not authenticated - please login")
    payload = decode_token(credentials.credentials)
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    user = get_user_by_id(int(user_id))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


async def get_current_user_if_required(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[UserPublic]:
    """If AUTH_REQUIRED=true, enforce auth. Else allow anonymous."""
    if not AUTH_REQUIRED:
        # Still try to parse user if token present, but don't require
        if credentials and credentials.credentials:
            try:
                payload = decode_token(credentials.credentials)
                uid = payload.get("sub")
                if uid:
                    u = get_user_by_id(int(uid))
                    return u
            except HTTPException:
                return None
        return None
    # Required path
    return await get_current_user(credentials)


# ── Simple in-memory rate limiter for auth endpoints ──────────────────────────
_login_attempts: dict[str, list[float]] = {}
RATE_LIMIT_WINDOW = 60.0  # seconds
RATE_LIMIT_MAX = 10  # attempts per window per IP


def check_rate_limit(key: str) -> None:
    now = time.monotonic()
    attempts = _login_attempts.get(key, [])
    # prune old
    attempts = [t for t in attempts if now - t < RATE_LIMIT_WINDOW]
    if len(attempts) >= RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="Too many attempts, try again later")
    attempts.append(now)
    _login_attempts[key] = attempts
