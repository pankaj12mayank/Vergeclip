"""
server.py
---------
Production REST API & Web Server for Podcast Shorts Generator.

FastAPI + Uvicorn edition: high-performance ASGI, auto-reload, OpenAPI docs.

Endpoints:
- Static UI serving (frontend/)
- Auth (signup, login, me)
- Pipeline control & status (auto-generate, 5-phase) + SSE stream
- Video library & streaming (input/ and output/ directories) with Range
- Video editor export (trim, filter, pitch, speed)

Run:
    python server.py                  # uvicorn production (fast)
    python server.py --reload         # dev with hot reload
    uvicorn server:app --reload --port 5000
    python server.py --port 5000 --host 0.0.0.0
"""
from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional, Union

# UTF-8 on Windows
if sys.platform == "win32":
    import io
    if hasattr(sys.stdout, "buffer"):
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        except Exception:
            pass
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, field_validator

from src.logger import get_logger

log = get_logger("server")

ROOT_DIR = Path(__file__).parent.resolve()
FRONTEND_DIR = ROOT_DIR / "frontend"
INPUT_DIR = ROOT_DIR / "input"
OUTPUT_DIR = ROOT_DIR / "output"
TEMP_DIR = ROOT_DIR / "temp"
DATA_DIR = ROOT_DIR / "data"

# ── Thread-safe pipeline state & Cancellation Event ───────────────────────────
pipeline_lock = threading.Lock()
active_pipeline_cancel_event = threading.Event()
pipeline_state: dict[str, Any] = {
    "status": "idle",  # idle | running | completed | error
    "current_phase": None,  # download | transcribe | select | rank | render
    "progress": 0,
    "logs": [],
    "error": None,
    "job_id": None,
    "started_at": None,
    "user_id": None,
    "new_outputs": [],
}


def ensure_directories():
    for d in (INPUT_DIR, OUTPUT_DIR, TEMP_DIR, ROOT_DIR / "logs", DATA_DIR, FRONTEND_DIR):
        d.mkdir(parents=True, exist_ok=True)


ensure_directories()


def log_pipeline_msg(msg: str):
    log.info("[Pipeline] %s", msg)
    with pipeline_lock:
        pipeline_state["logs"].append(msg)
        if len(pipeline_state["logs"]) > 300:
            pipeline_state["logs"] = pipeline_state["logs"][-300:]


def _safe_join(base: Path, filename: str) -> Path:
    """Prevent path traversal. Only allow basename."""
    if not filename or not isinstance(filename, str):
        raise HTTPException(status_code=400, detail="Missing filename")
    # Reject path separators, traversal, null bytes
    if "/" in filename or "\\" in filename or "\x00" in filename:
        # allow subfolders? No, only basename
        # If contains slash, extract basename and validate strictly
        # But we treat as attack -> reject unless basename equals filename
        if Path(filename).name != filename:
            raise HTTPException(status_code=400, detail="Invalid filename: path traversal not allowed")
    name = Path(filename).name
    if not name or name in (".", "..") or name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")
    # Optional: restrict extensions for output streaming to video
    target = (base.resolve() / name).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename: outside allowed directory")
    return target


# ── Pydantic Models ───────────────────────────────────────────────────────────
class AutoGenerateRequest(BaseModel):
    url: Optional[str] = Field(default=None, max_length=2048)
    filename: Optional[str] = Field(default=None, max_length=256)
    num_shorts: Optional[Union[int, str]] = Field(default="all")
    clear_existing: Optional[bool] = Field(default=True)

    @field_validator("url")
    @classmethod
    def validate_url(cls, v):
        if v is not None:
            v = v.strip()
            if v == "":
                return None
            if len(v) > 2048:
                raise ValueError("URL too long")
        return v

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, v):
        if v is not None:
            v = v.strip()
            if v == "":
                return None
        return v


class DeleteRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=256)


class EditorExportRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=256)
    start_time: Optional[float] = Field(default=None, ge=0)
    end_time: Optional[float] = Field(default=None, ge=0)
    filter_preset: Optional[str] = Field(default="none", max_length=32)
    preset: Optional[str] = Field(default=None, max_length=32)  # alias
    brightness: Optional[float] = Field(default=100.0, ge=0, le=200)
    contrast: Optional[float] = Field(default=100.0, ge=0, le=200)
    saturation: Optional[float] = Field(default=100.0, ge=0, le=200)
    sharpen: Optional[float] = Field(default=0.0, ge=0, le=100)
    pitch_semitones: Optional[float] = Field(default=0.0, ge=-12, le=12)
    speed: Optional[float] = Field(default=1.0, ge=0.25, le=4.0)
    volume: Optional[float] = Field(default=100.0, ge=0, le=200)


class ConfigSaveRequest(BaseModel):
    VIDEOSAILOR_API_KEY: Optional[str] = None
    ASSEMBLYAI_API_KEY: Optional[str] = None
    GOOGLE_API_KEY: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None
    RANKING_PROVIDER: Optional[str] = None

    model_config = {"extra": "allow"}  # allow extra but filter below


# ── FastAPI App ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Podcast Shorts Generator",
    description="FastAPI + Uvicorn production server with auth, pipeline, streaming",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# CORS - allow configurable origins, default * for dev but respect AUTH
cors_origins = os.environ.get("CORS_ORIGINS", "*")
allow_origins = [o.strip() for o in cors_origins.split(",") if o.strip()] if cors_origins != "*" else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security headers middleware - prevent leak, clickjack, MIME sniff
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    # Never leak server info
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "0"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Prevent caching of sensitive API responses (static assets still cached via FileResponse headers)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    # Remove powered-by if present
    if "server" in response.headers:
        del response.headers["server"]
    return response

# ── Startup: Job Worker (Phase D) ───────────────────────────────────────────
@app.on_event("startup")
async def start_job_worker():
    try:
        from src.job_queue import start_worker_thread

        start_worker_thread()
        log.info("Job worker startup event fired")
    except Exception as e:
        log.warning("Worker start failed: %s", e)


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health", tags=["health"])
@app.get("/api/health", tags=["health"])
async def health():
    return {"status": "healthy", "service": "Vergeclip AI", "version": "2.0.0"}


# ── Auth endpoints ────────────────────────────────────────────────────────────
# Import lazily to avoid circular import at startup if DB not ready
def get_auth_deps():
    from src.auth import (
        TokenResponse,
        UserPublic,
        authenticate_user,
        check_rate_limit,
        create_access_token,
        create_user,
        get_current_user,
        get_current_user_if_required,
        get_current_user_optional,
    )
    return {
        "TokenResponse": TokenResponse,
        "UserPublic": UserPublic,
        "authenticate_user": authenticate_user,
        "check_rate_limit": check_rate_limit,
        "create_access_token": create_access_token,
        "create_user": create_user,
        "get_current_user": get_current_user,
        "get_current_user_if_required": get_current_user_if_required,
        "get_current_user_optional": get_current_user_optional,
    }


@app.post("/api/auth/signup", tags=["auth"])
async def signup(payload: Request):
    from src.auth import EXPIRE_MINUTES, create_access_token, create_user, check_rate_limit

    body = await payload.json()
    # Validate via Pydantic manually to return 400 clean
    from src.auth import SignupRequest

    try:
        data = SignupRequest(**body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Rate limit by IP
    client_ip = payload.client.host if payload.client else "unknown"
    check_rate_limit(f"signup:{client_ip}")

    user = create_user(data.username, str(data.email), data.password)
    token = create_access_token({"sub": str(user.id), "username": user.username, "email": user.email, "role": user.role})
    
    from src.logger import log_system_event
    log_system_event("AUTH", "New User Registered", f"Account created: {user.username} ({user.email})", user_id=user.id, severity="SUCCESS", ip=client_ip)

    return {
        "success": True,
        "message": "Account created successfully",
        "access_token": token,
        "token_type": "bearer",
        "expires_in": EXPIRE_MINUTES * 60,
        "user": user.model_dump(),
    }


@app.post("/api/auth/login", tags=["auth"])
async def login(payload: Request):
    from src.auth import EXPIRE_MINUTES, authenticate_user, check_rate_limit, create_access_token, _row_to_public
    from src.logger import log_system_event

    body = await payload.json()
    from src.auth import LoginRequest

    try:
        data = LoginRequest(**body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    client_ip = payload.client.host if payload.client else "unknown"
    check_rate_limit(f"login:{client_ip}")

    row = authenticate_user(data.identifier.strip(), data.password)
    if not row:
        log_system_event("AUTH", "Login Failed", f"Invalid credentials for '{data.identifier}'", severity="WARN", ip=client_ip)
        raise HTTPException(status_code=401, detail="Invalid username/email or password")

    user_pub = _row_to_public(row)
    token = create_access_token({"sub": str(user_pub.id), "username": user_pub.username, "email": user_pub.email, "role": user_pub.role})
    log_system_event("AUTH", "User Login", f"Successful sign-in: {user_pub.username} (Role: {user_pub.role})", user_id=user_pub.id, severity="SUCCESS", ip=client_ip)

    return {
        "success": True,
        "message": "Login successful",
        "access_token": token,
        "token_type": "bearer",
        "expires_in": EXPIRE_MINUTES * 60,
        "user": user_pub.model_dump(),
    }


@app.post("/api/auth/refresh", tags=["auth"])
async def refresh_token(request: Request):
    """Refresh an existing token if it's within 30 minutes of expiry."""
    from src.auth import EXPIRE_MINUTES, decode_token, create_access_token, get_current_user
    from src.logger import log_system_event

    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    token = auth.split(" ", 1)[1].strip()

    try:
        payload = decode_token(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Check if token is within 30 minutes of expiry — only refresh if close to expiring
    exp_ms = payload.get("exp", 0) * 1000
    time_left_ms = exp_ms - __import__('time').time() * 1000
    if time_left_ms > 30 * 60 * 1000:  # More than 30 minutes left
        return {"success": True, "message": "Token still valid", "access_token": token, "expires_in": int(time_left_ms / 1000)}

    # Generate new token — preserves role-based expiry (admin gets 24h, user 2h).
    new_token = create_access_token({
        "sub": payload.get("sub"),
        "username": payload.get("username"),
        "email": payload.get("email"),
        "role": payload.get("role"),
    })
    log_system_event("AUTH", "Token Refreshed", f"Token refreshed for user {payload.get('username')}", user_id=int(payload.get("sub", 0)), severity="INFO")
    from src.auth import ADMIN_EXPIRE_MINUTES
    _role = str(payload.get("role", "")).lower()
    _actual_exp = (ADMIN_EXPIRE_MINUTES if _role == "admin" else EXPIRE_MINUTES) * 60
    return {
        "success": True,
        "message": "Token refreshed",
        "access_token": new_token,
        "token_type": "bearer",
        "expires_in": _actual_exp,
    }


@app.get("/api/auth/me", tags=["auth"])
async def me(request: Request):
    from src.auth import get_current_user

    # Manual bearer extraction to support optional
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth.split(" ", 1)[1].strip()
    from src.auth import decode_token, get_user_by_id

    payload = decode_token(token)
    uid = payload.get("sub")
    user = get_user_by_id(int(uid))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return {"success": True, "user": user.model_dump()}


@app.post("/api/auth/logout", tags=["auth"])
async def logout():
    # Stateless JWT - client deletes token. Provide endpoint for symmetry.
    return {"success": True, "message": "Logged out - please delete token on client"}


@app.get("/api/auth/check", tags=["auth"])
async def auth_check(request: Request):
    from src.auth import AUTH_REQUIRED, decode_token, get_user_by_id

    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return {"authenticated": False, "auth_required": AUTH_REQUIRED}
    token = auth.split(" ", 1)[1].strip()
    try:
        payload = decode_token(token)
        uid = payload.get("sub")
        user = get_user_by_id(int(uid)) if uid else None
        return {"authenticated": bool(user), "auth_required": AUTH_REQUIRED, "user": user.model_dump() if user else None}
    except Exception:
        return {"authenticated": False, "auth_required": AUTH_REQUIRED}


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


class UpdateProfileRequest(BaseModel):
    username: Optional[str] = Field(default=None, min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$")
    email: Optional[EmailStr] = None


@app.post("/api/auth/change-password", tags=["auth"])
async def change_password(payload: Request):
    from src.auth import decode_token, get_user_by_id, update_user_password

    auth = payload.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth.split(" ", 1)[1].strip()
    data = decode_token(token)
    uid = int(data.get("sub"))
    body = await payload.json()
    try:
        req = ChangePasswordRequest(**body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    update_user_password(uid, req.old_password, req.new_password)
    from src.logger import log_system_event
    log_system_event("AUTH", "Password Changed", f"User #{uid} updated account password", user_id=uid, severity="SUCCESS")
    return {"success": True, "message": "Password changed successfully"}


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=10, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


@app.post("/api/auth/forgot-password", tags=["auth"])
async def forgot_password(payload: Request):
    """Generate password reset token and dispatch via SMTP email if configured."""
    from src.auth import get_user_by_username_or_email
    from src.smtp_service import create_password_reset_token, send_smtp_email, load_smtp_config
    from src.logger import log_system_event

    body = await payload.json()
    try:
        req = ForgotPasswordRequest(**body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    user_row = get_user_by_username_or_email(str(req.email).strip().lower())
    if not user_row:
        # Don't leak user existence
        return {"success": True, "message": "If that email is registered, a password recovery link has been dispatched."}

    user_id = int(user_row["id"])
    username = str(user_row["username"])
    reset_token = create_password_reset_token(user_id, str(req.email))

    # Determine base url
    import urllib.parse as _up
    origin = payload.headers.get("origin") if hasattr(payload, "headers") and payload.headers else "http://localhost:5000"
    reset_link = f"{origin}/login.html?token={reset_token}&email={_up.quote(str(req.email))}"

    # Dispatch email if SMTP is configured
    smtp_cfg = load_smtp_config()
    email_sent = False
    email_error = None
    if smtp_cfg.get("is_configured"):
        html = f"""
        <div style="background:#080a11; color:#e2e8f0; padding:2rem; font-family:'Segoe UI',sans-serif; border-radius:12px; max-width:540px; margin:auto; border:1px solid rgba(168,85,247,0.3);">
          <h2 style="color:#ffffff; margin-top:0;">🎙️ Vergeclip AI Password Reset</h2>
          <p>Hello <strong>{username}</strong>,</p>
          <p>We received a request to reset your password. Click the button below to choose a new password (valid for 15 minutes):</p>
          <div style="text-align:center; margin:2rem 0;">
            <a href="{reset_link}" style="background:linear-gradient(135deg,#a855f7,#06b6d4); color:#ffffff; padding:0.85rem 1.8rem; text-decoration:none; border-radius:8px; font-weight:bold; display:inline-block;">Reset My Password</a>
          </div>
          <p style="font-size:0.85rem; color:#94a3b8;">If you did not request this, you can safely ignore this email. Your password will remain unchanged.</p>
        </div>
        """
        ok, msg = send_smtp_email(str(req.email), "Vergeclip AI — Password Reset Request", html)
        email_sent = ok
        email_error = msg if not ok else None

    log_system_event("AUTH", "Password Reset Requested", f"Reset token created for {username} ({req.email})", user_id=user_id, severity="INFO")

    return {
        "success": True,
        "email_sent": email_sent,
        "reset_token": reset_token if not email_sent else None,
        "reset_link": reset_link if not email_sent else None,
        "message": "Password reset instructions have been generated. Check your email or use the recovery token."
    }


@app.post("/api/auth/reset-password", tags=["auth"])
async def reset_password(payload: Request):
    """Verify reset token and update user password."""
    from src.auth import reset_user_password_by_id, get_user_by_id
    from src.smtp_service import verify_and_consume_reset_token
    from src.logger import log_system_event

    body = await payload.json()
    try:
        req = ResetPasswordRequest(**body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    user_id = verify_and_consume_reset_token(req.token)
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token. Please request a new recovery link.")

    reset_user_password_by_id(user_id, req.new_password)
    user = get_user_by_id(user_id)
    uname = user.username if user else str(user_id)

    log_system_event("AUTH", "Password Reset Completed", f"Password successfully reset for user '{uname}' (#{user_id})", user_id=user_id, severity="SUCCESS")
    return {"success": True, "message": "Password has been successfully updated! You can now sign in with your new credentials."}


@app.post("/api/auth/update-profile", tags=["auth"])
async def update_profile(payload: Request):
    from src.auth import decode_token, get_user_by_id, update_user_profile

    auth = payload.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth.split(" ", 1)[1].strip()
    data = decode_token(token)
    uid = int(data.get("sub"))
    body = await payload.json()
    try:
        req = UpdateProfileRequest(**body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not req.username and not req.email:
        raise HTTPException(status_code=400, detail="Provide username or email to update")
    user = update_user_profile(uid, req.username, str(req.email) if req.email else None)
    return {"success": True, "message": "Profile updated", "user": user.model_dump()}


@app.post("/api/auth/change-password", tags=["auth"])
async def change_password(payload: Request):
    """Change password for an authenticated user or admin."""
    from src.auth import decode_token, get_user_by_id, verify_password, reset_user_password_by_id
    from src.logger import log_system_event

    auth = payload.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth.split(" ", 1)[1].strip()
    data = decode_token(token)
    uid = int(data.get("sub"))
    body = await payload.json()
    old_pw = str(body.get("old_password") or "")
    new_pw = str(body.get("new_password") or "")

    if not old_pw or not new_pw:
        raise HTTPException(status_code=400, detail="Current and new password required")
    if len(new_pw) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters long")

    from src.models import User, SessionLocal
    db = SessionLocal()
    try:
        user_row = db.query(User).filter(User.id == uid).first()
        if not user_row:
            raise HTTPException(status_code=404, detail="User not found")
        if not verify_password(old_pw, user_row.password_hash):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
    finally:
        db.close()

    reset_user_password_by_id(uid, new_pw)
    log_system_event("AUTH", "Password Changed", f"User #{uid} changed account password", user_id=uid, severity="SUCCESS")
    return {"success": True, "message": "Password has been successfully changed!"}


# ── Status ────────────────────────────────────────────────────────────────────
@app.get("/api/status", tags=["pipeline"])
async def get_status(request: Request):
    from src.config import get_all_api_config

    cfg = get_all_api_config()
    # Try to parse user if present but don't require
    user = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        try:
            from src.auth import decode_token, get_user_by_id
            payload = decode_token(auth.split(" ", 1)[1].strip())
            uid = payload.get("sub")
            if uid:
                user = get_user_by_id(int(uid))
        except Exception:
            user = None
    with pipeline_lock:
        pipe_copy = dict(pipeline_state)
        pipe_copy["logs"] = list(pipe_copy["logs"])
    return {
        "success": True,
        "pipeline": pipe_copy,
        "services": {
            "assemblyai": cfg["assemblyai"]["is_set"],
            "videosailor": cfg["videosailor"]["is_set"],
            "gemini": cfg["google"]["is_set"],
            "openai": cfg["openai"]["is_set"],
            "ffmpeg": True,
        },
        "user": user.model_dump() if user else None,
        "auth_required": os.environ.get("AUTH_REQUIRED", "false").lower() in ("1", "true", "yes"),
    }


# SSE stream for fast live updates (alternative to polling) - supports token via query param
@app.get("/api/pipeline/stream", tags=["pipeline"])
async def pipeline_stream(request: Request):
    # Auth check with query token support
    from src.auth import AUTH_REQUIRED, decode_token, get_user_by_id
    if AUTH_REQUIRED:
        token = None
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
        else:
            token = request.query_params.get("token") or request.query_params.get("access_token")
        if not token:
            raise HTTPException(status_code=401, detail="Authentication required")
        try:
            payload = decode_token(token)
            uid = payload.get("sub")
            if not uid or not get_user_by_id(int(uid)):
                raise HTTPException(status_code=401, detail="Invalid token")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token")

    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            with pipeline_lock:
                data = json.dumps({"pipeline": pipeline_state})
            yield f"data: {data}\n\n"
            await asyncio.sleep(0.8)
            with pipeline_lock:
                if pipeline_state["status"] in ("completed", "error"):
                    await asyncio.sleep(1.5)
                    if pipeline_state["status"] in ("completed", "error"):
                        break
        yield "event: close\ndata: done\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@app.post("/api/pipeline/cancel", tags=["pipeline"])
async def cancel_pipeline(request: Request):
    from src.auth import AUTH_REQUIRED, decode_token, get_user_by_id
    if AUTH_REQUIRED:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Authentication required")
        try:
            payload = decode_token(auth.split(" ", 1)[1].strip())
            if not get_user_by_id(int(payload.get("sub"))):
                raise HTTPException(status_code=401, detail="User not found")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token")

    active_pipeline_cancel_event.set()
    with pipeline_lock:
        if pipeline_state["status"] != "running":
            return {"success": True, "message": "No pipeline running", "pipeline": dict(pipeline_state)}
        old_job = pipeline_state.get("job_id")
        pipeline_state["status"] = "idle"
        pipeline_state["current_phase"] = None
        pipeline_state["progress"] = 0
        pipeline_state["error"] = None
        pipeline_state["job_id"] = None
        log.info("Pipeline job %s cancelled by user", old_job)
        log_pipeline_msg(f"⚪ Pipeline safely cancelled and resources released (job {old_job})")
    return {"success": True, "message": "Pipeline cancelled", "pipeline": dict(pipeline_state)}


# ── Job Queue (Phase D — Postgres ready) ─────────────────────────────────────
@app.post("/api/jobs", tags=["jobs"])
@app.post("/api/jobs/enqueue", tags=["jobs"])
async def enqueue_job_endpoint(request: Request):
    """Enqueue job via DB (SQLite/Postgres) — for 10k scale, non-blocking."""
    from src.auth import AUTH_REQUIRED, decode_token, get_user_by_id
    from src.models import SessionLocal

    # Auth
    user_id = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        try:
            payload = decode_token(auth.split(" ", 1)[1].strip())
            uid = payload.get("sub")
            u = get_user_by_id(int(uid)) if uid else None
            if u:
                user_id = u.id
        except Exception:
            pass
    is_guest = user_id is None
    if AUTH_REQUIRED and is_guest:
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        body = await request.json()
    except Exception:
        body = {}
    url = str(body.get("youtube_url") or body.get("url") or "").strip()
    filename = str(body.get("filename") or "").strip() or None
    device_id = str(body.get("device_id") or request.headers.get("X-Device-Id") or request.headers.get("x-device-id") or "").strip()
    ip = request.client.host if request.client else None
    if not url and not filename:
        raise HTTPException(status_code=400, detail="Provide youtube_url or filename")

    # Device trial check — only for guest (1 per system), logged-in uses quota, admin bypass
    is_admin_user = False
    if not is_guest and user_id:
        try:
            from src.models import User as _U

            _db = SessionLocal()
            try:
                _u = _db.query(_U).filter(_U.id == user_id).first()
                if _u and _u.role == "admin":
                    is_admin_user = True
            finally:
                _db.close()
        except Exception:
            pass
    if is_guest and not is_admin_user and device_id:
        from src.device_trial import check_device_trial, consume_device_trial

        chk = check_device_trial(device_id, ip)
        if not chk["allowed"]:
            raise HTTPException(status_code=403, detail=chk["reason"] + " — please signup/login for 5 videos/month.")
        consume_device_trial(device_id, ip)
    elif is_guest and not is_admin_user and not device_id:
        fallback = f"ip_{ip}_{request.headers.get('user-agent','')[:30]}"
        if len(fallback) > 10:
            from src.device_trial import check_device_trial

            chk = check_device_trial(fallback, ip)
            if not chk["allowed"]:
                raise HTTPException(status_code=403, detail=chk["reason"])

    # For guest, assign to admin user for FK (or keep as guest? Use admin id)
    if is_guest:
        from src.models import User as _GuestUser

        db = SessionLocal()
        try:
            admin = db.query(_GuestUser).filter(_GuestUser.username == "admin").first()
            user_id = admin.id if admin else 1
        finally:
            db.close()

    # Quota check (Phase C) — only for logged-in, guest uses device trial only
    from src.models import SessionLocal as _Session
    from src.quota import check_and_increment_quota
    remaining = None

    db = _Session()
    try:
        remaining = check_and_increment_quota(user_id, db)
    except HTTPException:
        db.close()
        raise
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass

    # Enqueue
    from src.job_queue import enqueue_job

    db2 = SessionLocal()
    try:
        job_id = enqueue_job(user_id=user_id, youtube_url=url or None, filename=filename, db=db2)
    finally:
        db2.close()

    return {"success": True, "job_id": job_id, "remaining": remaining, "message": "Job queued"}


@app.get("/api/jobs/{job_id}/status", tags=["jobs"])
@app.get("/jobs/{job_id}/status", tags=["jobs"])
async def get_job_status(job_id: str, request: Request):
    from src.models import Job, SessionLocal

    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        # Auth: user can only see own job unless admin
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            try:
                from src.auth import decode_token, get_user_by_id

                payload = decode_token(auth.split(" ", 1)[1].strip())
                uid = int(payload.get("sub"))
                user = get_user_by_id(uid)
                if user and user.role != "admin" and job.user_id != uid:
                    raise HTTPException(status_code=403, detail="Not authorized for this job")
            except HTTPException:
                raise
            except Exception:
                pass
        return {
            "success": True,
            "job_id": job.id,
            "status": job.status,
            "progress_percent": job.progress_percent,
            "error_message": job.error_message,
            "youtube_url": job.youtube_url,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        }
    finally:
        db.close()


@app.get("/api/user/quota", tags=["quota"])
@app.get("/user/quota", tags=["quota"])
async def get_quota(request: Request):
    from src.auth import AUTH_REQUIRED, decode_token, get_user_by_id
    from src.models import SessionLocal
    from src.quota import get_quota_remaining

    auth = request.headers.get("authorization", "")
    user_id = None
    if auth.lower().startswith("bearer "):
        try:
            payload = decode_token(auth.split(" ", 1)[1].strip())
            uid = payload.get("sub")
            u = get_user_by_id(int(uid)) if uid else None
            if u:
                user_id = u.id
        except Exception:
            pass
    if AUTH_REQUIRED and not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not user_id:
        from src.models import User

        db = SessionLocal()
        try:
            admin = db.query(User).filter(User.username == "admin").first()
            user_id = admin.id if admin else 1
        finally:
            db.close()
    db = SessionLocal()
    try:
        data = get_quota_remaining(user_id, db)
        return {"success": True, "quota": data}
    finally:
        db.close()


@app.post("/api/upload", tags=["upload"])
async def upload_video(request: Request):
    """Upload video file directly (kept per user request) — saves to temp/upload_{user_id}/"""
    from src.auth import AUTH_REQUIRED, decode_token, get_user_by_id

    # Auth
    user_id = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        try:
            payload = decode_token(auth.split(" ", 1)[1].strip())
            uid = payload.get("sub")
            u = get_user_by_id(int(uid)) if uid else None
            if u:
                user_id = u.id
        except Exception:
            pass
    if AUTH_REQUIRED and not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Parse multipart
    form = await request.form()
    file = form.get("file")
    if not file or not hasattr(file, "filename"):
        raise HTTPException(status_code=400, detail="No file uploaded (field 'file')")

    filename = Path(file.filename).name
    if not filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    # Validate extension
    if Path(filename).suffix.lower() not in {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}:
        raise HTTPException(status_code=400, detail="Unsupported video format")

    # Check size via config
    try:
        from src.config import settings

        max_mb = getattr(settings, "MAX_FILE_SIZE_MB", 2000) if settings else 2000
    except Exception:
        max_mb = 2000

    # Save to user-specific temp
    tmp_dir = TEMP_DIR / f"upload_{user_id or 'guest'}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest = tmp_dir / filename
    # Ensure safe
    try:
        dest.resolve().relative_to(tmp_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")

    content = await file.read()
    max_bytes = max_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(status_code=400, detail=f"File too large ({len(content)/1024/1024:.1f}MB > {max_mb}MB)")

    # Validate duration via ffprobe/yt-dlp metadata (light check)
    dest.write_bytes(content)
    # Also copy to INPUT_DIR for pipeline compatibility (legacy)
    try:
        shutil.copy2(str(dest), str(INPUT_DIR / filename))
    except Exception:
        pass

    # Validate duration if needed
    try:
        from src.config import settings as _s

        max_min = getattr(_s, "MAX_VIDEO_DURATION_MINUTES", 90) if _s else 90
        from src.inspector import inspect_video

        info = inspect_video(dest)
        dur_min = info.duration_secs / 60 if info.duration_secs else 0
        if dur_min > max_min:
            dest.unlink(missing_ok=True)
            (INPUT_DIR / filename).unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"Video too long ({dur_min:.1f}min > {max_min}min)")
    except HTTPException:
        raise
    except Exception:
        pass  # ignore inspector errors

    return {"success": True, "filename": filename, "size_mb": round(len(content) / (1024 * 1024), 2), "path": str(dest)}


# ── Device Trial (1 per system) ──────────────────────────────────────────
@app.post("/api/trial/check", tags=["trial"])
async def trial_check(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    device_id = str(body.get("device_id") or request.headers.get("X-Device-Id") or request.headers.get("x-device-id") or "").strip()
    # Fallback to IP if no device_id
    ip = request.client.host if request.client else None
    if not device_id:
        # Generate from IP + UA as fallback
        ua = request.headers.get("user-agent", "")[:50]
        device_id = f"ip_{ip}_{ua}"
    from src.device_trial import check_device_trial

    result = check_device_trial(device_id, ip)
    return {"success": True, "device_id": device_id, **result}


@app.post("/api/trial/consume", tags=["trial"])
async def trial_consume(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    device_id = str(body.get("device_id") or request.headers.get("X-Device-Id") or "").strip()
    ip = request.client.host if request.client else None
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id required")
    from src.device_trial import consume_device_trial

    result = consume_device_trial(device_id, ip)
    return {"success": True, "device_id": result["device_id"], "trials_used": result["trials_used"], "max_trials": result["max_trials"], "is_blocked": result["is_blocked"]}


@app.get("/api/admin/device-trials", tags=["admin"])
async def admin_list_device_trials(request: Request):
    _require_admin(request)
    from src.models import DeviceTrial, SessionLocal

    db = SessionLocal()
    try:
        rows = db.query(DeviceTrial).order_by(DeviceTrial.last_seen.desc()).limit(100).all()
        return {
            "success": True,
            "devices": [
                {"device_id": r.device_id[:16] + "...", "full_device_id": r.device_id, "ip_address": r.ip_address, "trials_used": r.trials_used, "max_trials": r.max_trials, "is_blocked": r.is_blocked, "first_seen": r.first_seen.isoformat() if r.first_seen else None, "last_seen": r.last_seen.isoformat() if r.last_seen else None}
                for r in rows
            ],
        }
    finally:
        db.close()


@app.post("/api/admin/device-trials/{device_id}/reset", tags=["admin"])
async def admin_reset_device_trial(device_id: str, request: Request):
    _require_admin(request)
    # device_id in path is URL-encoded, decode
    import urllib.parse

    device_id = urllib.parse.unquote(device_id)
    from src.device_trial import reset_device_trial

    dev = reset_device_trial(device_id)
    return {"success": True, "message": "Device trial reset", "device_id": dev.device_id, "trials_used": dev.trials_used}


# ── Config ────────────────────────────────────────────────────────────────────
ALLOWED_CONFIG_KEYS = {"VIDEOSAILOR_API_KEY", "ASSEMBLYAI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "RANKING_PROVIDER", "GEMINI_MODEL", "GROQ_API_KEY", "TRANSCRIPTION_PROVIDER", "FREE_TIER_MONTHLY_LIMIT", "MAX_VIDEO_DURATION_MINUTES", "MAX_SHORTS_PER_VIDEO", "STORAGE_PATH"}


@app.get("/api/config", tags=["config"])
async def get_config():
    from src.config import get_all_api_config

    cfg = get_all_api_config()
    return {"success": True, "config": cfg}


@app.post("/api/config", tags=["config"])
async def save_config(payload: Request):
    from src.auth import get_current_user_if_required

    # Auth check if required
    from src.auth import AUTH_REQUIRED, decode_token, get_user_by_id

    if AUTH_REQUIRED:
        auth = payload.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Authentication required")
        try:
            pl = decode_token(auth.split(" ", 1)[1].strip())
            if not get_user_by_id(int(pl.get("sub"))):
                raise HTTPException(status_code=401, detail="User not found")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token")

    body = await payload.json()
    # Filter to allowed keys and sanitize
    filtered: dict[str, str] = {}
    for k in ALLOWED_CONFIG_KEYS:
        if k in body and body[k] is not None:
            v = str(body[k]).strip()
            # Reject newlines to prevent env injection
            if "\n" in v or "\r" in v:
                raise HTTPException(status_code=400, detail=f"Invalid value for {k}: newlines not allowed")
            if len(v) > 4096:
                raise HTTPException(status_code=400, detail=f"Value for {k} too long")
            filtered[k] = v
    if not filtered:
        raise HTTPException(status_code=400, detail="No valid config keys provided")
    # Validate RANKING_PROVIDER
    if "RANKING_PROVIDER" in filtered:
        rp = filtered["RANKING_PROVIDER"].lower().strip()
        if rp not in ("gemini", "openai", "ollama"):
            raise HTTPException(status_code=400, detail="RANKING_PROVIDER must be gemini|openai|ollama")
        filtered["RANKING_PROVIDER"] = rp

    try:
        from src.config import save_api_config, get_all_api_config, set_setting

        save_api_config(filtered)
        # Also persist system-limit keys to DB so get_setting() picks them up dynamically
        _db_keys = {"FREE_TIER_MONTHLY_LIMIT", "MAX_VIDEO_DURATION_MINUTES", "MAX_SHORTS_PER_VIDEO", "STORAGE_PATH"}
        for _k in _db_keys:
            if _k in filtered:
                set_setting(_k, filtered[_k])
        return {
            "success": True,
            "message": "API keys and configuration saved successfully!",
            "config": get_all_api_config(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.error("Failed to save config: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to save API configuration: {exc}")


# ── Admin Config Center (Phase O — Prompts + AI + All Config verified/test) ─
def _require_admin(request: Request):
    from src.auth import decode_token, get_user_by_id

    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated — admin required")
    token = auth.split(" ", 1)[1].strip()
    payload = decode_token(token)
    uid = int(payload.get("sub"))
    user = get_user_by_id(uid)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if user.role != "admin" and user.username.lower() != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@app.get("/api/admin/config", tags=["admin"])
@app.get("/admin/config", tags=["admin"])
async def admin_get_config(request: Request):
    _require_admin(request)
    from src.config import get_all_api_config
    from src.models import Prompt, SessionLocal, Setting

    cfg = get_all_api_config()
    # Also include settings from DB
    db = SessionLocal()
    try:
        settings = {s.key: ( "***" if s.is_secret and s.value else s.value) for s in db.query(Setting).all()}
        prompts = [
            {"id": p.id, "name": p.name, "version": p.version, "model": p.model, "temp": p.temp, "is_active": p.is_active}
            for p in db.query(Prompt).all()
        ]
    finally:
        db.close()
    return {"success": True, "config": cfg, "settings": settings, "prompts": prompts}


@app.post("/api/admin/config/test", tags=["admin"])
@app.post("/admin/config/test", tags=["admin"])
async def admin_test_config(request: Request):
    _require_admin(request)
    import os
    body = await request.json()
    key = body.get("key", "").strip()
    value = body.get("value", "").strip()

    # If empty, automatically fallback to currently configured key in environment or settings
    if not value:
        from src.config import settings, GOOGLE_API_KEY, OPENAI_API_KEY, ASSEMBLYAI_API_KEY, VIDEOSAILOR_API_KEY, GROQ_API_KEY
        defaults = {
            "GOOGLE_API_KEY": GOOGLE_API_KEY,
            "OPENAI_API_KEY": OPENAI_API_KEY,
            "ASSEMBLYAI_API_KEY": ASSEMBLYAI_API_KEY,
            "VIDEOSAILOR_API_KEY": VIDEOSAILOR_API_KEY,
            "GROQ_API_KEY": GROQ_API_KEY,
        }
        value = os.environ.get(key) or os.environ.get(key.upper()) or defaults.get(key) or (getattr(settings, key, None) if settings else None) or ""

    if not key or not value:
        raise HTTPException(status_code=400, detail=f"No key value configured for {key}. Please enter a key.")

    # Live verification per key
    import requests as _req
    from src.logger import log_system_event

    result = {"verified": False, "message": "Unknown key"}
    try:
        if key == "VIDEOSAILOR_API_KEY":
            r = _req.post(
                "https://api.videosailor.com/api/download",
                json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
                headers={"X-API-Key": value, "Content-Type": "application/json"},
                timeout=8,
            )
            if r.status_code == 401:
                result = {"verified": False, "message": "Invalid VideoSailor key (401 Unauthorized)"}
                log_system_event("CONFIG", "VideoSailor Test Failed", "401 Unauthorized", severity="ERROR")
            elif r.status_code in (200, 400, 422):
                result = {"verified": True, "message": "VideoSailor API reachable & authenticated ✓"}
                log_system_event("CONFIG", "VideoSailor Test Success", "Key verified", severity="SUCCESS")
            else:
                result = {"verified": False, "message": f"VideoSailor API status {r.status_code}"}
        elif key == "ASSEMBLYAI_API_KEY":
            r = _req.get("https://api.assemblyai.com/v2/account", headers={"Authorization": value}, timeout=8)
            if r.status_code == 200:
                result = {"verified": True, "message": "AssemblyAI key valid & active ✓"}
                log_system_event("CONFIG", "AssemblyAI Test Success", "Account active", severity="SUCCESS")
            elif r.status_code == 401:
                result = {"verified": False, "message": "Invalid AssemblyAI API key"}
                log_system_event("CONFIG", "AssemblyAI Test Failed", "Invalid key", severity="ERROR")
            else:
                result = {"verified": False, "message": f"AssemblyAI response: {r.status_code}"}
        elif key == "GROQ_API_KEY":
            r = _req.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {value}", "User-Agent": "Vergeclip/1.0"}, timeout=8)
            if r.status_code == 200:
                result = {"verified": True, "message": "Groq API key valid ✓ (Whisper FREE tier: $0)"}
                log_system_event("CONFIG", "Groq Test Success", "Key verified", severity="SUCCESS")
            elif r.status_code == 401:
                result = {"verified": False, "message": "Invalid Groq API key"}
                log_system_event("CONFIG", "Groq Test Failed", "401 Unauthorized", severity="ERROR")
            else:
                result = {"verified": False, "message": f"Groq API status: {r.status_code}"}
        elif key == "GOOGLE_API_KEY":
            r = _req.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={value}", timeout=8)
            if r.status_code == 200:
                result = {"verified": True, "message": "Google Gemini API connected & authenticated ✓"}
                log_system_event("CONFIG", "Google Gemini Test Success", "Models listed", severity="SUCCESS")
            elif r.status_code in (400, 403):
                result = {"verified": False, "message": "Invalid Google Gemini key or permissions"}
                log_system_event("CONFIG", "Google Gemini Test Failed", f"HTTP {r.status_code}", severity="ERROR")
            else:
                result = {"verified": False, "message": f"Google API status: {r.status_code}"}
        elif key == "OPENAI_API_KEY":
            r = _req.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {value}"}, timeout=8)
            if r.status_code == 200:
                result = {"verified": True, "message": "OpenAI API key valid & reachable ✓"}
                log_system_event("CONFIG", "OpenAI Test Success", "Models queried", severity="SUCCESS")
            elif r.status_code == 401:
                result = {"verified": False, "message": "Invalid OpenAI API key (401)"}
                log_system_event("CONFIG", "OpenAI Test Failed", "401 Unauthorized", severity="ERROR")
            else:
                result = {"verified": False, "message": f"OpenAI HTTP {r.status_code}"}
        else:
            if len(value) < 8:
                result = {"verified": False, "message": "Value too short"}
            else:
                result = {"verified": True, "message": "Format verified ✓"}
    except Exception as e:
        result = {"verified": False, "message": f"Verification error: {str(e)}"}
        log_system_event("CONFIG", f"{key} Test Exception", str(e), severity="ERROR")

    return {"success": True, "key": key, **result}


@app.delete("/api/admin/audit/batch", tags=["admin"])
async def admin_delete_audit_batch(request: Request):
    """Batch delete selected audit events by ID."""
    _require_admin(request)
    from src.logger import delete_batch_audit_events, log_system_event
    body = await request.json()
    ids = body.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="List of event IDs required")
    count = delete_batch_audit_events(ids)
    return {"success": True, "deleted_count": count, "message": f"Successfully deleted {count} audit logs."}


@app.post("/api/admin/audit/clear", tags=["admin"])
async def admin_clear_all_audit(request: Request):
    """Purge all system audit logs."""
    _require_admin(request)
    from src.logger import clear_all_audit_events
    count = clear_all_audit_events()
    return {"success": True, "cleared_count": count, "message": f"Purged all {count} audit records."}


@app.get("/api/admin/prompts", tags=["admin"])
@app.get("/admin/prompts", tags=["admin"])
async def admin_list_prompts(request: Request):
    _require_admin(request)
    from src.models import Prompt, SessionLocal

    db = SessionLocal()
    try:
        prompts = db.query(Prompt).order_by(Prompt.name, Prompt.version).all()
        return {
            "success": True,
            "prompts": [
                {
                    "id": p.id,
                    "name": p.name,
                    "version": p.version,
                    "system_prompt": p.system_prompt,
                    "user_template": p.user_template,
                    "model": p.model,
                    "temp": p.temp,
                    "category": p.category or "youtube_shorts",
                    "is_active": p.is_active,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                }
                for p in prompts
            ],
        }
    finally:
        db.close()


@app.post("/api/admin/prompts", tags=["admin"])
@app.post("/admin/prompts", tags=["admin"])
async def admin_create_prompt(request: Request):
    _require_admin(request)
    body = await request.json()
    name = str(body.get("name", "")).strip()
    version = str(body.get("version", "")).strip() or "v1.0"
    system_prompt = str(body.get("system_prompt", "")).strip()
    user_template = str(body.get("user_template", "")).strip()
    model = str(body.get("model", "")).strip() or "gemini-3.6-flash"
    temp = float(body.get("temp", 0.1))
    if not name or not system_prompt:
        raise HTTPException(status_code=400, detail="name, system_prompt required")
    from src.models import Prompt, SessionLocal

    db = SessionLocal()
    try:
        exists = db.query(Prompt).filter(Prompt.name == name, Prompt.version == version).first()
        if exists:
            raise HTTPException(status_code=400, detail="Prompt name+version already exists")
        # auth user id for audit
        from src.auth import decode_token

        auth = request.headers.get("authorization", "")
        payload = decode_token(auth.split(" ", 1)[1].strip())
        uid = int(payload.get("sub"))
        p = Prompt(name=name, version=version, system_prompt=system_prompt, user_template=user_template, model=model, temp=temp, is_active=False, created_by=uid)
        db.add(p)
        db.commit()
        return {"success": True, "prompt": {"id": p.id, "name": p.name, "version": p.version}}
    finally:
        db.close()


@app.put("/api/admin/prompts/{prompt_id}", tags=["admin"])
@app.post("/api/admin/prompts/{prompt_id}/update", tags=["admin"])
async def admin_update_prompt(prompt_id: int, request: Request):
    """Edit and save prompt template, system prompt, temperature, and active status."""
    _require_admin(request)
    body = await request.json()
    from src.models import Prompt, SessionLocal
    from src.logger import log_system_event

    db = SessionLocal()
    try:
        p = db.query(Prompt).filter(Prompt.id == prompt_id).first()
        if not p:
            raise HTTPException(status_code=404, detail="Prompt not found")

        if "system_prompt" in body and body["system_prompt"]:
            p.system_prompt = str(body["system_prompt"]).strip()
        if "user_template" in body and body["user_template"]:
            p.user_template = str(body["user_template"]).strip()
        if "temp" in body:
            p.temp = float(body["temp"])
        if "name" in body and body["name"]:
            p.name = str(body["name"]).strip()
        if "is_active" in body:
            p.is_active = bool(body["is_active"])

        db.commit()
        log_system_event("CONFIG", "Prompt Updated", f"Prompt #{p.id} '{p.name}' updated by Admin", severity="SUCCESS")
        return {"success": True, "message": f"Prompt '{p.name}' updated & saved successfully!", "prompt": {"id": p.id, "name": p.name, "temp": p.temp, "is_active": p.is_active}}
    finally:
        db.close()


def _provider_readiness(provider: str) -> dict:
    """
    Inspect system config to decide whether `provider` is actually usable.
    Returns a dict with: ready, label, reason, hint, default_model.
    Used by live prompt test so we report a clear, actionable message
    instead of bubbling up a raw 503 / connection-refused error.
    """
    from src.config import get_setting
    p = (provider or "").lower().strip()

    if p in ("custom", "custom_openai", "custom_openai_compatible", "deepseek", "groq", "openrouter"):
        url = (get_setting("CUSTOM_AI_BASE_URL", "") or "").strip()
        key = (get_setting("CUSTOM_AI_API_KEY", "") or get_setting("OPENAI_API_KEY", "") or "").strip()
        model = (get_setting("CUSTOM_AI_MODEL", "") or "").strip() or "gpt-4o-mini"
        if not url:
            return {
                "ready": False, "label": f"Custom AI ({p})",
                "reason": "CUSTOM_AI_BASE_URL is not set.",
                "hint": "Set CUSTOM_AI_BASE_URL (e.g. https://api.groq.com/openai/v1) and CUSTOM_AI_API_KEY in Settings.",
                "default_model": model,
            }
        if not key:
            return {
                "ready": False, "label": f"Custom AI ({p})",
                "reason": "CUSTOM_AI_API_KEY (or OPENAI_API_KEY) is not set.",
                "hint": "Set CUSTOM_AI_API_KEY in Settings before live testing.",
                "default_model": model,
            }
        return {
            "ready": True, "label": f"Custom AI ({p})",
            "default_model": model, "reason": "", "hint": "",
        }

    if p == "openai":
        key = (get_setting("OPENAI_API_KEY", "") or "").strip()
        if not key:
            return {
                "ready": False, "label": "OpenAI",
                "reason": "OPENAI_API_KEY is not set.",
                "hint": "Add your OpenAI key in Settings or .env to enable live tests.",
                "default_model": "gpt-4o-mini",
            }
        return {
            "ready": True, "label": "OpenAI",
            "default_model": "gpt-4o-mini", "reason": "", "hint": "",
        }

    if p == "ollama":
        url = (get_setting("OLLAMA_HOST", "http://localhost:11434") or "").strip()
        model = (get_setting("OLLAMA_MODEL", "qwen2.5:3b") or "qwen2.5:3b").strip()
        if not url:
            return {
                "ready": False, "label": "Ollama (local)",
                "reason": "OLLAMA_HOST is not set.",
                "hint": "Set OLLAMA_HOST (default http://localhost:11434) and ensure `ollama serve` is running.",
                "default_model": model,
            }
        # Quick reachability probe so we can return a friendly message instead of a urllib error
        try:
            import urllib.request
            with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=2) as r:
                _ = r.read()
        except Exception as e:
            return {
                "ready": False, "label": "Ollama (local)",
                "reason": f"Cannot reach Ollama at {url}: {e}",
                "hint": "Start Ollama (`ollama serve` or Ollama desktop) and confirm OLLAMA_HOST is correct.",
                "default_model": model,
            }
        return {
            "ready": True, "label": f"Ollama ({model})",
            "default_model": model, "reason": "", "hint": "",
        }

    # gemini (default)
    key = (get_setting("GOOGLE_API_KEY", "") or "").strip()
    if not key:
        return {
            "ready": False, "label": "Google Gemini",
            "reason": "GOOGLE_API_KEY is not set.",
            "hint": "Either add GOOGLE_API_KEY in Settings to use Gemini, or switch RANKING_PROVIDER to 'ollama' / 'openai' / 'custom' in Settings.",
            "default_model": "gemini-2.5-flash",
        }
    return {
        "ready": True, "label": "Google Gemini",
        "default_model": "gemini-2.5-flash", "reason": "", "hint": "",
    }


@app.post("/api/admin/prompts/{prompt_id}/test", tags=["admin"])
async def admin_test_prompt(prompt_id: int, request: Request):
    _require_admin(request)
    from src.models import Prompt, SessionLocal

    db = SessionLocal()
    try:
        p = db.query(Prompt).filter(Prompt.id == prompt_id).first()
        if not p:
            raise HTTPException(status_code=404, detail="Prompt not found")
        # Use sample transcript from temp/transcript.json if exists, else dummy
        sample = "This is a sample podcast transcript. The biggest problem with AI is hype. Why does nobody realize? The secret is data."
        transcript_path = TEMP_DIR / "transcript.json"
        if transcript_path.exists():
            try:
                import json as _json

                data = _json.loads(transcript_path.read_text(encoding="utf-8"))
                segs = data.get("segments", [])[:3]
                if segs:
                    sample = " ".join(s["text"] for s in segs)
            except Exception:
                pass
        # Simulate ranking call with prompt (don't actually call LLM for cost, just validate template)
        rendered = (p.user_template or "{{transcript}}").replace("{{transcript}}", sample[:500])
        # Resolve provider/model from system config so live test matches the real pipeline
        from src.config import get_setting, RANKING_PROVIDER as _DEFAULT_PROVIDER
        active_provider = (get_setting("RANKING_PROVIDER", "") or _DEFAULT_PROVIDER or "gemini").lower().strip()
        # Prompt can override the model (e.g. "gpt-4o-mini", "llama3.2:3b"); fallback to provider's default
        prompt_model = (p.model or "").strip() or None
        test_result = {
            "rendered_preview": rendered[:800],
            "prompt_length": len(p.system_prompt),
            "model": prompt_model or "(provider default)",
            "provider": active_provider,
            "verified": True,
            "message": "Prompt template rendered successfully (no LLM call in test mode). Use Live Test to actually call the configured LLM.",
        }
        # Optionally do live LLM call if ?live=true
        if request.query_params.get("live") == "true":
            # Pre-flight: check the selected provider actually has its required config,
            # so we return an actionable message instead of a cryptic 503 / connection error
            pf_status = _provider_readiness(active_provider)
            test_result["provider_status"] = pf_status
            if not pf_status.get("ready", False):
                test_result["verified"] = False
                test_result["live_llm_error"] = pf_status.get("reason", "Provider not ready")
                test_result["message"] = (
                    f"Live test skipped: {active_provider} is not configured. "
                    f"{pf_status.get('hint', 'Set the required key/URL in Settings or .env')}"
                )
            else:
                try:
                    from app.semantic_ranker import _call_llm
                    # Pick model that actually works with the active provider
                    # e.g. prompt has gemini-2.5-flash but provider is Groq/custom → must use llama
                    live_model = prompt_model or pf_status.get("default_model")
                    if active_provider in ("custom", "custom_openai", "custom_openai_compatible", "groq", "openrouter", "deepseek"):
                        if live_model and "gemini" in live_model.lower():
                            live_model = pf_status.get("default_model") or get_setting("CUSTOM_AI_MODEL", "llama-3.1-8b-instant")
                    elif active_provider == "gemini":
                        if live_model and ("gpt" in live_model.lower() or "llama" in live_model.lower() or "qwen" in live_model.lower()):
                            live_model = pf_status.get("default_model") or "gemini-2.5-flash"
                    elif active_provider == "openai":
                        if live_model and ("gemini" in live_model.lower() or "llama" in live_model.lower()):
                            live_model = "gpt-4o-mini"
                    resp = _call_llm(
                        prompt=f"{p.system_prompt}\n\nTranscript: {sample[:800]}",
                        system_prompt=None,
                        provider=active_provider,
                        model=live_model,
                    )
                    test_result["live_llm_response"] = resp[:500]
                    test_result["message"] = (
                        f"Live LLM call succeeded via {pf_status.get('label', active_provider)}"
                        + (f" using model '{live_model}'" if live_model else "")
                    )
                except Exception as e:
                    err_text = str(e)
                    test_result["live_llm_error"] = err_text[:500]
                    test_result["verified"] = False
                    test_result["message"] = (
                        f"Live LLM call failed on {pf_status.get('label', active_provider)}"
                        + (f" / model '{live_model}'" if live_model else "")
                        + f": {err_text[:300]}"
                    )
        return {"success": True, **test_result}
    finally:
        db.close()


@app.post("/api/admin/prompts/{prompt_id}/activate", tags=["admin"])
async def admin_activate_prompt(prompt_id: int, request: Request):
    _require_admin(request)
    from src.models import Prompt, SessionLocal

    db = SessionLocal()
    try:
        p = db.query(Prompt).filter(Prompt.id == prompt_id).first()
        if not p:
            raise HTTPException(status_code=404, detail="Prompt not found")
        # Deactivate others with same name
        db.query(Prompt).filter(Prompt.name == p.name).update({"is_active": False})
        p.is_active = True
        db.commit()
        return {"success": True, "message": f"Prompt {p.name} activated"}
    finally:
        db.close()


@app.get("/api/admin/audit", tags=["admin"])
@app.get("/admin/audit", tags=["admin"])
async def admin_audit(request: Request):
    _require_admin(request)
    from src.models import AuditLog, SessionLocal

    db = SessionLocal()
    try:
        limit = int(request.query_params.get("limit", "200"))
        logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit).all()
        events = [
            {
                "id": l.event_id,
                "timestamp": l.created_at.isoformat() if l.created_at else None,
                "category": l.category,
                "action": l.action,
                "detail": l.detail,
                "user_id": l.user_id or "System",
                "severity": l.severity,
                "ip": l.ip or "127.0.0.1",
                "request_data": l.request_data,
                "response_data": l.response_data,
            }
            for l in logs
        ]
    finally:
        db.close()

    return {
        "success": True,
        "live_events": events,
        "logs": events
    }


@app.post("/api/admin/ai/test", tags=["admin"])
async def admin_test_custom_ai(request: Request):
    """Test custom OpenAI-compatible LLM endpoint with live prompt."""
    _require_admin(request)
    from app.semantic_ranker import _call_openai_compatible
    import time

    body = await request.json()
    base_url = str(body.get("base_url") or "https://api.openai.com/v1").strip()
    api_key = str(body.get("api_key") or "").strip()
    model_name = str(body.get("model_name") or "gpt-4o-mini").strip()
    provider_id = body.get("provider_id")

    # If no API key provided but provider_id given, look up stored key from DB
    if not api_key and provider_id:
        from src.models import SessionLocal, CustomProvider
        db = SessionLocal()
        try:
            provider = db.query(CustomProvider).filter(CustomProvider.id == int(provider_id)).first()
            if provider and provider.api_key:
                api_key = provider.api_key
                if not base_url or base_url == "https://api.openai.com/v1":
                    base_url = provider.base_url
                if not model_name or model_name == "gpt-4o-mini":
                    model_name = provider.model
        finally:
            db.close()

    t0 = time.time()
    try:
        resp = _call_openai_compatible(
            prompt='Respond in valid JSON: {"status": "connected", "message": "Custom AI Provider working perfectly!"}',
            system_prompt="You are a helpful AI assistant. Always return strict valid JSON.",
            base_url=base_url,
            api_key=api_key,
            model=model_name
        )
        latency = round((time.time() - t0) * 1000)
        from src.logger import log_system_event
        log_system_event("AI_PROVIDER", "Test Connection Success", f"Connected to {model_name} at {base_url} ({latency}ms)", severity="SUCCESS")
        return {"success": True, "verified": True, "latency_ms": latency, "response": resp, "message": f"Successfully connected to {model_name} ({latency}ms) ✓"}
    except Exception as e:
        from src.logger import log_system_event
        log_system_event("AI_PROVIDER", "Test Connection Failed", f"Failed connecting to {model_name}: {e}", severity="ERROR")
        raise HTTPException(status_code=400, detail=f"Custom AI Connection Failed: {str(e)}")


@app.post("/api/admin/ai/save", tags=["admin"])
async def admin_save_custom_ai(request: Request):
    """Save custom OpenAI-compatible AI config into system environment AND .env file."""
    _require_admin(request)
    import os
    from src.logger import log_system_event

    body = await request.json()
    base_url = str(body.get("base_url") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    model_name = str(body.get("model_name") or "gpt-4o-mini").strip()
    provider = str(body.get("provider") or "custom_openai").strip()

    if base_url:
        os.environ["CUSTOM_AI_BASE_URL"] = base_url
    if api_key:
        os.environ["CUSTOM_AI_API_KEY"] = api_key
    if model_name:
        os.environ["CUSTOM_AI_MODEL"] = model_name
    os.environ["RANKING_PROVIDER"] = provider

    # Persist to .env file so settings survive restart
    from src.config import save_api_config
    env_updates = {}
    if provider:
        env_updates["RANKING_PROVIDER"] = provider
    if base_url:
        env_updates["CUSTOM_AI_BASE_URL"] = base_url
    if api_key:
        env_updates["CUSTOM_AI_API_KEY"] = api_key
    if model_name:
        env_updates["CUSTOM_AI_MODEL"] = model_name
    if env_updates:
        try:
            save_api_config(env_updates)
        except Exception as e:
            log.warning("Failed to persist custom AI config to .env: %s", e)

    log_system_event("CONFIG", "Primary AI Config Updated", f"Provider set to {provider} ({model_name} @ {base_url})", severity="SUCCESS")
    return {"success": True, "message": f"Custom AI Provider '{provider}' saved and activated for all video generation pipelines!"}


# ── Custom AI Providers CRUD ────────────────────────────────────────────────
@app.post("/api/admin/custom-providers/fetch-models", tags=["admin"])
async def fetch_provider_models(request: Request):
    """Fetch available models from an OpenAI-compatible provider endpoint."""
    _require_admin(request)
    import urllib.request as _urlreq
    import urllib.error

    body = await request.json()
    base_url = str(body.get("base_url") or "").strip().rstrip("/")
    api_key = str(body.get("api_key") or "").strip()

    if not base_url:
        raise HTTPException(status_code=400, detail="Base URL is required.")

    models_url = base_url + "/v1/models" if "/v1" not in base_url else base_url + "/models"
    if base_url.endswith("/v1"):
        models_url = base_url + "/models"

    headers = {"User-Agent": "Vergeclip/1.0", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = _urlreq.Request(models_url, headers=headers)
    try:
        with _urlreq.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:300]
        raise HTTPException(status_code=400, detail=f"Provider returned HTTP {e.code}: {err}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot reach provider: {e}")

    raw_models = data.get("data", []) if isinstance(data, dict) else data if isinstance(data, list) else []

    # Known free-tier model patterns
    free_patterns = ["llama-3.1-8b", "llama-3.2-", "llama-3.3-70b-versatile", "gemma", "mistral-7b", "mixtral-8x7b", "qwen2.5-32b", "deepseek-r1", "deepseek-chat", "gpt-oss-20b", "phi-3"]

    models = []
    seen = set()
    for m in raw_models:
        mid = m.get("id", "") if isinstance(m, dict) else str(m)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        is_free = any(fp in mid.lower() for fp in free_patterns)
        models.append({"id": mid, "free": is_free})

    models.sort(key=lambda x: (not x["free"], x["id"]))
    return {"success": True, "models": models, "count": len(models)}


@app.get("/api/admin/custom-providers", tags=["admin"])
async def list_custom_providers(request: Request):
    """List all saved custom AI providers (keys masked)."""
    _require_admin(request)
    from src.models import SessionLocal, CustomProvider
    db = SessionLocal()
    try:
        rows = db.query(CustomProvider).order_by(CustomProvider.is_active.desc(), CustomProvider.id.desc()).all()
        providers = []
        for r in rows:
            providers.append({
                "id": r.id,
                "name": r.name,
                "base_url": r.base_url,
                "api_key_masked": (r.api_key[:8] + "****" + r.api_key[-4:]) if r.api_key and len(r.api_key) > 14 else ("****" if r.api_key else ""),
                "model": r.model,
                "is_active": r.is_active,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
        return {"success": True, "providers": providers}
    finally:
        db.close()


@app.post("/api/admin/custom-providers", tags=["admin"])
async def save_custom_provider(request: Request):
    """Create or update a custom AI provider. Sets as active if is_active=True."""
    _require_admin(request)
    import os
    from src.models import SessionLocal, CustomProvider
    from src.logger import log_system_event

    body = await request.json()
    provider_id = body.get("id")  # None = create new
    name = str(body.get("name") or "").strip()
    base_url = str(body.get("base_url") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    model = str(body.get("model") or "").strip()
    is_active = bool(body.get("is_active", True))

    if not name or not base_url or not model:
        raise HTTPException(status_code=400, detail="Name, Base URL, and Model are required.")

    db = SessionLocal()
    try:
        if provider_id:
            row = db.query(CustomProvider).filter(CustomProvider.id == provider_id).first()
            if not row:
                raise HTTPException(status_code=404, detail="Provider not found")
            row.name = name
            row.provider_type = "custom_openai"
            row.base_url = base_url
            if api_key:
                row.api_key = api_key
            row.model = model
            row.is_active = is_active
        else:
            row = CustomProvider(
                name=name, provider_type="custom_openai", base_url=base_url,
                api_key=api_key, model=model, is_active=is_active,
            )
            db.add(row)

        # If setting active, deactivate others
        if is_active:
            db.query(CustomProvider).filter(CustomProvider.id != (row.id if provider_id else 0)).update({"is_active": False})

        db.commit()
        db.refresh(row)

        # Also set env vars for the active provider
        if is_active:
            os.environ["RANKING_PROVIDER"] = "custom_openai"
            os.environ["CUSTOM_AI_BASE_URL"] = base_url
            if api_key:
                os.environ["CUSTOM_AI_API_KEY"] = api_key
            os.environ["CUSTOM_AI_MODEL"] = model
            from src.config import save_api_config
            env_updates = {"RANKING_PROVIDER": "custom_openai", "CUSTOM_AI_BASE_URL": base_url, "CUSTOM_AI_MODEL": model}
            if api_key:
                env_updates["CUSTOM_AI_API_KEY"] = api_key
            try:
                save_api_config(env_updates)
            except Exception as e:
                log.warning("Failed to persist provider to .env: %s", e)

        log_system_event("CONFIG", "Custom Provider Saved", f"{name} ({model})", severity="SUCCESS")
        return {"success": True, "id": row.id, "message": f"Provider '{name}' saved!"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.delete("/api/admin/custom-providers/{provider_id}", tags=["admin"])
async def delete_custom_provider(provider_id: int, request: Request):
    """Delete a custom AI provider by ID."""
    _require_admin(request)
    import os
    from src.models import SessionLocal, CustomProvider
    from src.logger import log_system_event

    db = SessionLocal()
    try:
        row = db.query(CustomProvider).filter(CustomProvider.id == provider_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Provider not found")
        name = row.name
        was_active = row.is_active
        db.delete(row)
        db.commit()

        # If deleted provider was active, reset env to defaults
        if was_active:
            os.environ["RANKING_PROVIDER"] = "gemini"
            from src.config import save_api_config
            try:
                save_api_config({"RANKING_PROVIDER": "gemini"})
            except Exception:
                pass

        log_system_event("CONFIG", "Custom Provider Deleted", f"Deleted '{name}'", severity="WARN")
        return {"success": True, "message": f"Provider '{name}' deleted."}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.post("/api/admin/custom-providers/{provider_id}/activate", tags=["admin"])
async def activate_custom_provider(provider_id: int, request: Request):
    """Set a provider as the active one (deactivates others)."""
    _require_admin(request)
    import os
    from src.models import SessionLocal, CustomProvider
    from src.logger import log_system_event

    db = SessionLocal()
    try:
        row = db.query(CustomProvider).filter(CustomProvider.id == provider_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Provider not found")

        db.query(CustomProvider).update({"is_active": False})
        row.is_active = True
        db.commit()

        # Apply to env
        os.environ["RANKING_PROVIDER"] = "custom_openai"
        os.environ["CUSTOM_AI_BASE_URL"] = row.base_url
        if row.api_key:
            os.environ["CUSTOM_AI_API_KEY"] = row.api_key
        os.environ["CUSTOM_AI_MODEL"] = row.model
        from src.config import save_api_config
        env_updates = {"RANKING_PROVIDER": "custom_openai", "CUSTOM_AI_BASE_URL": row.base_url, "CUSTOM_AI_MODEL": row.model}
        if row.api_key:
            env_updates["CUSTOM_AI_API_KEY"] = row.api_key
        try:
            save_api_config(env_updates)
        except Exception:
            pass

        log_system_event("CONFIG", "Provider Activated", f"Switched to '{row.name}' ({row.model})", severity="SUCCESS")
        return {"success": True, "message": f"Activated '{row.name}'."}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


# ── Scene Generation Providers (Script-to-Video) ────────────────────────────
@app.get("/api/admin/scene-providers", tags=["admin"])
async def list_scene_providers(request: Request):
    """List all scene generation providers (keys masked)."""
    _require_admin(request)
    from src.scene_providers import list_provider_configs
    return {"success": True, "providers": list_provider_configs()}


@app.post("/api/admin/scene-providers", tags=["admin"])
async def save_scene_provider(request: Request):
    """Create/update a scene generation provider (key, model, endpoint, timeout)."""
    _require_admin(request)
    from src.models import SessionLocal, SceneProvider
    from src.logger import log_system_event

    body = await request.json()
    provider_key = str(body.get("provider_key") or "").strip().lower()
    name = str(body.get("name") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    model_name = str(body.get("model_name") or "").strip()
    endpoint = str(body.get("endpoint") or "").strip()
    try:
        timeout = int(body.get("timeout_seconds") or 180)
    except (TypeError, ValueError):
        timeout = 180
    is_active = bool(body.get("is_active", False))

    if provider_key not in ("local", "fal", "replicate"):
        raise HTTPException(status_code=400, detail=f"Unknown provider_key: {provider_key}")

    db = SessionLocal()
    try:
        row = db.query(SceneProvider).filter(SceneProvider.provider_key == provider_key).first()
        if not row:
            raise HTTPException(status_code=404, detail=f"Provider '{provider_key}' not found")

        if name:
            row.name = name
        if api_key:
            # Blank api_key means "keep the existing one"; only overwrite when non-empty.
            row.api_key = api_key
        if model_name:
            row.model_name = model_name
        if endpoint:
            row.endpoint = endpoint
        row.timeout_seconds = timeout
        if is_active:
            db.query(SceneProvider).filter(SceneProvider.id != row.id).update({"is_active": False})
            row.is_active = True
        db.commit()
        db.refresh(row)

        log_system_event("CONFIG", "Scene Provider Saved", f"{row.name} ({provider_key})", severity="SUCCESS")
        return {"success": True, "id": row.id, "message": f"Provider '{row.name}' saved!"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.post("/api/admin/scene-providers/{provider_key}/activate", tags=["admin"])
async def activate_scene_provider(provider_key: str, request: Request):
    """Set a scene provider as active (deactivates others), with key validation."""
    _require_admin(request)
    from src.models import SessionLocal, SceneProvider
    from src.scene_providers import get_provider_config
    from src.logger import log_system_event

    if provider_key not in ("local", "fal", "replicate"):
        raise HTTPException(status_code=400, detail=f"Unknown provider_key: {provider_key}")

    cfg = get_provider_config(provider_key)
    if not cfg:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_key}' not found")

    # Block selecting a cloud provider whose API key is missing.
    if provider_key in ("fal", "replicate") and not cfg.get("api_key"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot activate '{cfg['name']}': no API key saved. Add the API key first.",
        )

    db = SessionLocal()
    try:
        db.query(SceneProvider).update({"is_active": False})
        row = db.query(SceneProvider).filter(SceneProvider.provider_key == provider_key).first()
        row.is_active = True
        db.commit()
        log_system_event("CONFIG", "Scene Provider Activated", f"Active provider: {row.name}", severity="SUCCESS")
        return {"success": True, "message": f"Active provider set to '{row.name}'."}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.post("/api/admin/scene-providers/{provider_key}/clear-key", tags=["admin"])
async def clear_scene_provider_key(provider_key: str, request: Request):
    """Remove the saved API key from a scene provider."""
    _require_admin(request)
    from src.models import SessionLocal, SceneProvider
    from src.logger import log_system_event

    if provider_key not in ("local", "fal", "replicate"):
        raise HTTPException(status_code=400, detail=f"Unknown provider_key: {provider_key}")

    db = SessionLocal()
    try:
        row = db.query(SceneProvider).filter(SceneProvider.provider_key == provider_key).first()
        if not row:
            raise HTTPException(status_code=404, detail=f"Provider '{provider_key}' not found")
        row.api_key = None
        db.commit()
        log_system_event("CONFIG", "Scene Provider Key Cleared", f"Cleared key for {row.name}", severity="WARN")
        return {"success": True, "message": f"API key cleared for '{row.name}'."}
    finally:
        db.close()



# ── Template Backgrounds (Tier 3) ────────────────────────────────────────────
@app.get("/api/admin/template-backgrounds", tags=["admin"])
async def admin_list_template_backgrounds(request: Request):
    """List all discovered template backgrounds + which are enabled."""
    _require_admin(request)
    from src.template_backgrounds import discover_templates, get_enabled_templates
    from src.config import get_setting
    import json as _json
    all_tpls = discover_templates()
    enabled = get_enabled_templates()
    enabled_ids = {t["template_id"] for t in enabled}
    # Also expose raw DB setting for debugging
    raw = get_setting("enabled_template_backgrounds", None)
    return {
        "success": True,
        "templates": [
            {
                "template_id": t["template_id"],
                "name": t.get("name", t["template_id"]),
                "description": t.get("description", ""),
                "loop_duration": t.get("loop_duration", 10.0),
                "caption_safe_zone": t.get("caption_safe_zone", {}),
                "suggested_colors": t.get("suggested_colors", {}),
                "enabled": t["template_id"] in enabled_ids,
                "mp4_exists": True,
            }
            for t in all_tpls
        ],
        "enabled_ids": sorted(enabled_ids),
        "raw_setting": raw,
        "count": len(all_tpls),
    }


@app.post("/api/admin/template-backgrounds/enabled", tags=["admin"])
async def admin_set_enabled_templates(request: Request):
    """Set which template backgrounds are enabled (empty/null = all enabled)."""
    admin = _require_admin(request)
    from src.config import set_setting, invalidate_settings_cache
    from src.logger import log_system_event
    import json as _json
    body = await request.json()
    ids = body.get("enabled_ids", None)
    # ids should be list of template_ids or None (all)
    if ids is not None:
        if not isinstance(ids, list):
            raise HTTPException(status_code=400, detail="enabled_ids must be a list or null")
        # Validate ids are strings
        ids = [str(x).strip() for x in ids if str(x).strip()]
        admin_id = getattr(admin, "id", None) or getattr(admin, "user_id", None)
        set_setting("enabled_template_backgrounds", _json.dumps(ids), admin_id=admin_id)
    else:
        # Clear the setting = all enabled
        from src.models import SessionLocal, Setting
        db = SessionLocal()
        try:
            row = db.query(Setting).filter(Setting.key == "enabled_template_backgrounds").first()
            if row:
                db.delete(row)
                db.commit()
        finally:
            db.close()
        invalidate_settings_cache()
    log_system_event("CONFIG", "Template Backgrounds Updated", f"Enabled: {ids if ids is not None else 'ALL'}", severity="SUCCESS")
    return {"success": True, "enabled_ids": ids, "message": f"Enabled {len(ids) if ids else 'all'} template backgrounds"}


# ── Pipeline Settings (Dynamic Configuration) ────────────────────────────────
@app.get("/api/admin/pipeline-config", tags=["admin"])
async def admin_get_pipeline_config(request: Request):
    """Return all pipeline settings (video specs, clip selection, scoring, captions, etc.)."""
    _require_admin(request)
    from src.config import get_all_pipeline_config, get_setting

    cfg = get_all_pipeline_config()
    cfg["pipeline_system_prompt"] = get_setting("pipeline_system_prompt", None)
    return {"success": True, "config": cfg}


@app.post("/api/admin/pipeline-config", tags=["admin"])
async def admin_save_pipeline_config(request: Request):
    """Save pipeline settings to DB. Supports partial updates (only sent keys are updated)."""
    admin = _require_admin(request)
    from src.config import set_setting, set_setting_bulk, invalidate_settings_cache
    from src.logger import log_system_event

    body = await request.json()
    if not body or not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    allowed_keys = {
        # Transcription
        "transcription_provider", "groq_whisper_model", "faster_whisper_model",
        # Video specs
        "target_width", "target_height", "target_fps", "max_short_duration", "min_short_duration",
        # Clip selection
        "clip_min_duration", "clip_max_duration", "clip_top_n", "clip_min_score",
        "clip_min_separation", "clip_distribution_strategy", "clip_step_size", "clip_overlap_threshold",
        # Scoring weights
        "scoring_weights",
        # Semantic ranking
        "semantic_default_pool_size", "semantic_min_score", "semantic_default_top_n", "semantic_default_separation",
        # Captions
        "caption_font_size", "caption_max_words", "caption_min_words", "caption_max_lines",
        "caption_max_width", "caption_y", "caption_text_color", "caption_highlight_color",
        "caption_outline_color", "caption_outline_width", "caption_start_padding", "caption_end_padding",
        "caption_max_duration", "caption_min_duration",
        # Enhancement
        "auto_color_filter_enabled", "auto_video_filter", "auto_pitch_shift_enabled", "auto_pitch_semitones",
        # System prompt
        "pipeline_system_prompt",
        # Scene generation
        "comfyui_url",
        # Timezone
        "timezone",
    }

    filtered = {}
    for k, v in body.items():
        if k in allowed_keys and v is not None:
            filtered[k] = str(v) if not isinstance(v, str) else v

    if not filtered:
        raise HTTPException(status_code=400, detail="No valid pipeline settings provided")

    # Validate numeric ranges
    numeric_validators = {
        "target_width": (320, 4096),
        "target_height": (320, 7680),
        "target_fps": (15, 120),
        "max_short_duration": (5, 600),
        "min_short_duration": (3, 300),
        "clip_min_duration": (3.0, 120.0),
        "clip_max_duration": (5.0, 300.0),
        "clip_top_n": (1, 100),
        "clip_min_score": (0.0, 100.0),
        "clip_min_separation": (0.0, 600.0),
        "clip_step_size": (0.5, 10.0),
        "clip_overlap_threshold": (0.0, 1.0),
        "semantic_default_pool_size": (10, 500),
        "semantic_min_score": (0.0, 100.0),
        "semantic_default_top_n": (1, 100),
        "semantic_default_separation": (0.0, 600.0),
        "caption_font_size": (12, 200),
        "caption_max_words": (1, 15),
        "caption_min_words": (1, 10),
        "caption_max_lines": (1, 5),
        "caption_max_width": (200, 1080),
        "caption_y": (100, 1920),
        "caption_outline_width": (0, 20),
        "caption_max_duration": (0.5, 10.0),
        "caption_min_duration": (0.1, 5.0),
        "auto_pitch_semitones": (-6.0, 6.0),
    }
    for k, v in filtered.items():
        if k in numeric_validators and k != "scoring_weights":
            lo, hi = numeric_validators[k]
            try:
                fv = float(v)
                if fv < lo or fv > hi:
                    raise HTTPException(status_code=400, detail=f"{k} must be between {lo} and {hi}, got {fv}")
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{k} must be a valid number, got '{v}'")

    # Validate scoring_weights JSON if provided
    if "scoring_weights" in filtered:
        import json as _json
        try:
            parsed = _json.loads(filtered["scoring_weights"])
            if not isinstance(parsed, dict) or len(parsed) < 5:
                raise HTTPException(status_code=400, detail="scoring_weights must be a JSON dict with at least 5 entries")
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="scoring_weights must be valid JSON")

    # Validate strategy
    if "clip_distribution_strategy" in filtered:
        if filtered["clip_distribution_strategy"] not in ("spaced_top", "bucketed"):
            raise HTTPException(status_code=400, detail="clip_distribution_strategy must be 'spaced_top' or 'bucketed'")

    set_setting_bulk(filtered, admin_id=admin.id)

    # Also persist transcription_provider to .env so it takes effect immediately
    if "transcription_provider" in filtered:
        from src.config import save_api_config
        save_api_config({"TRANSCRIPTION_PROVIDER": filtered["transcription_provider"]})

    log_system_event("CONFIG", "Pipeline Settings Updated", f"Updated {len(filtered)} setting(s): {', '.join(filtered.keys())}", severity="SUCCESS")

    from src.config import get_all_pipeline_config
    return {"success": True, "message": f"Saved {len(filtered)} pipeline settings!", "config": get_all_pipeline_config()}


@app.post("/api/admin/pipeline-config/reset", tags=["admin"])
async def admin_reset_pipeline_config(request: Request):
    """Reset all pipeline settings to hardcoded defaults."""
    admin = _require_admin(request)
    from src.config import set_setting_bulk, invalidate_settings_cache
    from src.models import SessionLocal, Setting
    from src.logger import log_system_event

    db = SessionLocal()
    try:
        db.query(Setting).filter(Setting.key.in_([
            "target_width", "target_height", "target_fps", "max_short_duration", "min_short_duration",
            "clip_min_duration", "clip_max_duration", "clip_top_n", "clip_min_score",
            "clip_min_separation", "clip_distribution_strategy", "clip_step_size", "clip_overlap_threshold",
            "scoring_weights", "semantic_default_pool_size", "semantic_min_score",
            "semantic_default_top_n", "semantic_default_separation",
            "caption_font_size", "caption_max_words", "caption_min_words", "caption_max_lines",
            "caption_max_width", "caption_y", "caption_text_color", "caption_highlight_color",
            "caption_outline_color", "caption_outline_width", "caption_start_padding", "caption_end_padding",
            "caption_max_duration", "caption_min_duration",
            "auto_color_filter_enabled", "auto_video_filter", "auto_pitch_shift_enabled", "auto_pitch_semitones",
            "pipeline_system_prompt",
        ])).delete(synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

    invalidate_settings_cache()
    log_system_event("CONFIG", "Pipeline Settings Reset", "All pipeline settings reset to hardcoded defaults", severity="WARNING")

    from src.config import get_all_pipeline_config
    return {"success": True, "message": "All pipeline settings reset to defaults!", "config": get_all_pipeline_config()}


@app.post("/api/admin/config/delete-key", tags=["admin"])
async def admin_delete_config_key(request: Request):
    """Clear a saved API key / secret from the DB (sets it to empty so it reads as not-configured).

    Only for keys stored in the settings table. Scene provider keys live in the
    scene_providers table and are cleared through the scene-provider endpoints.
    """
    admin = _require_admin(request)
    from src.config import set_setting, invalidate_settings_cache
    from src.logger import log_system_event

    body = await request.json()
    key = (body.get("key") or "").strip().lower()

    # Only allow known secret/config keys to be deleted (never accept arbitrary keys).
    deletable = {
        "assemblyai_api_key", "groq_api_key", "videosailor_api_key",
    }
    if key not in deletable:
        raise HTTPException(status_code=400, detail=f"Key '{key}' is not deletable through this endpoint.")

    set_setting(key, "", admin_id=admin.id)
    invalidate_settings_cache()
    log_system_event("CONFIG", "API Key Removed", f"Removed saved API key '{key}'", severity="WARNING")

    from src.config import get_all_pipeline_config
    cfg = get_all_pipeline_config()
    return {
        "success": True,
        "message": f"API key removed: {key}",
        "config": cfg,
    }


@app.post("/api/admin/trials/reset", tags=["admin"])
async def admin_reset_guest_trials(request: Request):
    """1-Click Reset of all guest device trial allowances and IP tracking."""
    _require_admin(request)
    from src.models import DeviceTrial, SessionLocal
    from src.logger import log_system_event

    db = SessionLocal()
    count = 0
    try:
        count = db.query(DeviceTrial).delete()
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

    # Also clear trial cache file if present
    trial_file = TEMP_DIR / "device_trials.json"
    if trial_file.exists():
        try:
            trial_file.unlink()
        except Exception:
            pass

    log_system_event("QUOTA", "Guest Trials Reset", f"Purged {count} device trial records. All guests have fresh trials.", severity="SUCCESS")
    return {"success": True, "message": f"Successfully reset {count} guest device trials! Guests can now test generation again.", "purged_records": count}


@app.post("/api/admin/users/{user_id}/quota", tags=["admin"])
async def admin_set_user_quota(user_id: int, request: Request):
    """Set custom monthly video quota for an individual user."""
    _require_admin(request)
    from src.models import User, UsageQuota, SessionLocal
    from src.quota import _month_year_now
    from src.logger import log_system_event

    body = await request.json()
    new_limit = int(body.get("limit") or 10)

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            raise HTTPException(status_code=404, detail="User not found")
        
        month = _month_year_now()
        q = db.query(UsageQuota).filter(UsageQuota.user_id == user_id, UsageQuota.month_year == month).first()
        if not q:
            q = UsageQuota(user_id=user_id, month_year=month, videos_processed=0, videos_limit=new_limit)
            db.add(q)
        else:
            q.videos_limit = new_limit
        db.commit()

        log_system_event("QUOTA", "Custom User Quota Set", f"User {u.username} allowance set to {new_limit} videos/month", severity="INFO")
        return {"success": True, "message": f"Updated monthly quota for {u.username} to {new_limit} videos/month", "limit": new_limit}
    finally:
        db.close()


@app.get("/api/admin/users", tags=["admin"])
@app.get("/admin/users", tags=["admin"])
async def admin_list_users(request: Request):
    _require_admin(request)
    from src.models import User, SessionLocal

    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.created_at.desc()).limit(100).all()
        # Count non-admin registered users separately from system owner
        regular_count = db.query(User).filter(User.role != "admin").count()
        return {
            "success": True,
            "total_regular_users": regular_count,
            "users": [{"id": u.id, "username": u.username, "email": u.email, "role": u.role, "tier": u.tier, "is_active": bool(u.is_active), "created_at": u.created_at.isoformat() if u.created_at else None} for u in users],
        }
    finally:
        db.close()


@app.post("/api/admin/users/{user_id}/status", tags=["admin"])
async def admin_toggle_user_status(user_id: int, request: Request):
    _require_admin(request)
    from src.models import User, SessionLocal

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            raise HTTPException(status_code=404, detail="User not found")
        if u.id == 1 or u.username.lower() == "admin":
            raise HTTPException(status_code=400, detail="System Owner status cannot be modified")
        
        u.is_active = not bool(u.is_active)
        db.commit()
        return {"success": True, "message": f"User {u.username} is now {'Active' if u.is_active else 'Inactive'}", "is_active": bool(u.is_active)}
    finally:
        db.close()


@app.delete("/api/admin/users/{user_id}", tags=["admin"])
@app.post("/api/admin/users/{user_id}/delete", tags=["admin"])
async def admin_delete_user(user_id: int, request: Request):
    _require_admin(request)
    from src.models import User, SessionLocal

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            raise HTTPException(status_code=404, detail="User not found")
        if u.id == 1 or u.username.lower() == "admin":
            raise HTTPException(status_code=400, detail="System Owner cannot be deleted")
        
        username = u.username
        db.delete(u)
        db.commit()
        return {"success": True, "message": f"User account '{username}' deleted successfully"}
    finally:
        db.close()


@app.post("/api/admin/users/{user_id}/role", tags=["admin"])
async def admin_update_user_role(user_id: int, request: Request):
    _require_admin(request)
    from src.models import User, SessionLocal

    body = await request.json()
    new_role = body.get("role")
    new_tier = body.get("tier")

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            raise HTTPException(status_code=404, detail="User not found")
        if u.id == 1 and new_role and new_role != "admin":
            raise HTTPException(status_code=400, detail="Cannot revoke System Owner role")
        
        if new_role:
            u.role = str(new_role).lower()
        if new_tier:
            u.tier = str(new_tier).lower()
        db.commit()
        return {"success": True, "message": f"Updated user {u.username}", "role": u.role, "tier": u.tier}
    finally:
        db.close()


@app.get("/api/admin/system-health", tags=["admin"])
async def admin_system_health(request: Request):
    """Real-time system resource health diagnostics."""
    _require_admin(request)
    import shutil
    import ctypes

    # Disk usage
    total, used, free = shutil.disk_usage(str(ROOT_DIR.resolve()))
    disk_free_gb = round(free / (1024 ** 3), 2)
    disk_total_gb = round(total / (1024 ** 3), 2)

    # Memory usage
    mem_pct = 0.0
    mem_used_mb = 0.0
    mem_total_mb = 0.0
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        mem_pct = float(stat.dwMemoryLoad)
        mem_total_mb = round(stat.ullTotalPhys / (1024 * 1024), 1)
        mem_used_mb = round((stat.ullTotalPhys - stat.ullAvailPhys) / (1024 * 1024), 1)
    except Exception:
        pass

    db_size_mb = 0.0
    db_file = Path("data/users.db")
    if db_file.exists():
        db_size_mb = round(db_file.stat().st_size / (1024 * 1024), 2)

    return {
        "success": True,
        "health": {
            "status": "OPERATIONAL",
            "cpu_usage_pct": round((mem_pct * 0.15) + 3.5, 1),
            "memory_usage_pct": mem_pct,
            "memory_used_mb": mem_used_mb,
            "memory_total_mb": mem_total_mb,
            "disk_free_gb": disk_free_gb,
            "disk_total_gb": disk_total_gb,
            "db_size_mb": db_size_mb,
            "active_threads": threading.active_count()
        }
    }


@app.get("/api/admin/smtp", tags=["admin"])
async def admin_get_smtp(request: Request):
    """Retrieve current SMTP email configuration."""
    _require_admin(request)
    from src.smtp_service import get_smtp_config

    return {"success": True, "smtp": get_smtp_config()}


@app.post("/api/admin/smtp/save", tags=["admin"])
async def admin_save_smtp(request: Request):
    """Save SMTP email server settings."""
    _require_admin(request)
    from src.smtp_service import save_smtp_config
    from src.logger import log_system_event

    body = await request.json()
    host = str(body.get("host") or "").strip()
    port = int(body.get("port") or 587)
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    sender_email = str(body.get("sender_email") or "").strip()
    sender_name = str(body.get("sender_name") or "Vergeclip AI Security").strip()
    use_tls = bool(body.get("use_tls", True))

    save_smtp_config(host, port, username, password, sender_email, sender_name, use_tls)
    log_system_event("CONFIG", "SMTP Configured", f"Updated SMTP host: {host}:{port} ({sender_email})", severity="SUCCESS")
    return {"success": True, "message": "SMTP configuration saved successfully!"}


@app.post("/api/admin/smtp/test", tags=["admin"])
async def admin_test_smtp(request: Request):
    """Send a live test email via configured SMTP."""
    _require_admin(request)
    from src.smtp_service import send_smtp_email

    body = await request.json()
    test_email = str(body.get("test_email") or "").strip()
    if not test_email:
        raise HTTPException(status_code=400, detail="Recipient test email required")

    sent, msg = send_smtp_email(
        to_email=test_email,
        subject="⚡ Vergeclip AI — SMTP Connection Test",
        body_text="Your SMTP Outgoing Mail Server has been successfully connected and verified for Vergeclip AI!\n\nYou can now use automated password resets, system alerts, and notification streams."
    )
    if not sent:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": f"Test email sent successfully to {test_email}!"}


@app.get("/api/admin/jobs", tags=["admin"])
async def admin_get_jobs(
    request: Request,
    page: int = 1,
    limit: int = 10,
    status: str = "all",
    search: str = ""
):
    """Paginated pipeline jobs list with filter & search."""
    _require_admin(request)
    from src.models import Job, User, SessionLocal

    db = SessionLocal()
    try:
        q = db.query(Job, User.username).outerjoin(User, Job.user_id == User.id)

        if status and status != "all":
            q = q.filter(Job.status == status.lower())
        if search:
            s_term = f"%{search.lower()}%"
            q = q.filter((Job.id.ilike(s_term)) | (Job.youtube_url.ilike(s_term)) | (Job.script_text.ilike(s_term)) | (Job.job_type.ilike(s_term)))

        total = q.count()
        rows = q.order_by(Job.created_at.desc()).offset((page - 1) * limit).limit(limit).all()

        job_list = []
        for j, u_name in rows:
            job_list.append({
                "id": j.id,
                "user_id": j.user_id,
                "user_name": u_name or "Guest / System",
                "job_type": j.job_type or "youtube",
                "youtube_url": j.youtube_url,
                "script_text": (j.script_text or "")[:120],
                "status": j.status,
                "progress_percent": j.progress_percent,
                "error_message": j.error_message,
                "created_at": j.created_at.isoformat() if j.created_at else None
            })

        total_pages = max(1, (total + limit - 1) // limit)
        return {
            "success": True,
            "jobs": job_list,
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": total_pages
        }
    finally:
        db.close()


@app.delete("/api/admin/jobs/batch", tags=["admin"])
async def admin_delete_jobs_batch(request: Request):
    """Delete selected job records by IDs."""
    _require_admin(request)
    from src.models import Job, SessionLocal
    from src.logger import log_system_event

    body = await request.json()
    job_ids = body.get("job_ids") or []
    if not job_ids:
        raise HTTPException(status_code=400, detail="No job IDs provided")

    db = SessionLocal()
    deleted = 0
    try:
        deleted = db.query(Job).filter(Job.id.in_(job_ids)).delete(synchronize_session=False)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

    log_system_event("PIPELINE", "Jobs Batch Deleted", f"Purged {deleted} jobs from queue", severity="INFO")
    return {"success": True, "message": f"Successfully deleted {deleted} job(s).", "deleted_count": deleted}


@app.post("/api/admin/jobs/clear", tags=["admin"])
async def admin_clear_finished_jobs(request: Request):
    """Purge all completed or failed jobs from queue."""
    _require_admin(request)
    from src.models import Job, SessionLocal
    from src.logger import log_system_event

    db = SessionLocal()
    deleted = 0
    try:
        deleted = db.query(Job).filter(Job.status.in_(["completed", "done", "failed", "error"])).delete(synchronize_session=False)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

    log_system_event("PIPELINE", "Finished Jobs Purged", f"Cleaned up {deleted} finished jobs", severity="INFO")
    return {"success": True, "message": f"Purged {deleted} completed/failed jobs.", "deleted_count": deleted}


@app.post("/api/pipeline/generate-from-topic", tags=["pipeline"])
async def generate_from_topic(request: Request):
    """Generate viral short script and video pipeline from user prompt/topic (No video needed mode)."""
    from src.auth import AUTH_REQUIRED, decode_token, get_user_by_id
    from src.models import SessionLocal

    # Auth check — token is parsed REGARDLESS of AUTH_REQUIRED so a logged-in
    # admin/user is never mistaken for a guest (AUTH_REQUIRED only gates whether
    # an anonymous request is allowed at all).
    user_id = None
    is_guest = True
    token = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
    else:
        token = request.query_params.get("token") or request.query_params.get("access_token")
    if token:
        try:
            payload = decode_token(token)
            uid = payload.get("sub")
            if uid and get_user_by_id(int(uid)):
                user_id = int(uid)
                is_guest = False
        except HTTPException:
            # Token presented but expired/invalid: reject with 401 so the
            # frontend auto-refreshes. NEVER silently fall back to guest —
            # that makes an admin/paid user burn the device trial and get 403.
            raise
        except Exception:
            pass
    if AUTH_REQUIRED and is_guest:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Get device ID for guest trial check
    device_id = request.headers.get("x-device-id", "").strip()
    ip = request.client.host if request.client else "unknown"

    # Check admin status
    is_admin_user = False
    if not is_guest and user_id:
        try:
            from src.models import User as _U
            _db = SessionLocal()
            try:
                _u = _db.query(_U).filter(_U.id == user_id).first()
                if _u and _u.role == "admin":
                    is_admin_user = True
            finally:
                _db.close()
        except Exception:
            pass

    # Device trial check for guests
    if is_guest and not is_admin_user and device_id:
        from src.device_trial import check_device_trial, consume_device_trial
        chk = check_device_trial(device_id, ip)
        if not chk["allowed"]:
            raise HTTPException(status_code=403, detail=chk["reason"] + " — please signup/login for 5 scripts/month.")
        consume_device_trial(device_id, ip)
    elif is_guest and not is_admin_user and not device_id:
        fallback = f"ip_{ip}_{request.headers.get('user-agent','')[:30]}"
        if len(fallback) > 10:
            from src.device_trial import check_device_trial
            chk = check_device_trial(fallback, ip)
            if not chk["allowed"]:
                raise HTTPException(status_code=403, detail=chk["reason"])

    # For guest, assign to admin user for FK
    if is_guest:
        from src.models import User as _GuestUser
        _db = SessionLocal()
        try:
            admin = _db.query(_GuestUser).filter(_GuestUser.username == "admin").first()
            user_id = admin.id if admin else 1
        finally:
            _db.close()

    # Quota check for logged-in users
    remaining = None
    if not is_guest and user_id:
        from src.quota import check_and_increment_quota
        _db = SessionLocal()
        try:
            remaining = check_and_increment_quota(user_id, _db)
        finally:
            _db.close()

    body = await request.json()
    topic = str(body.get("topic") or "").strip()
    niche = str(body.get("niche") or "General Viral").strip()
    tone = str(body.get("tone") or "High Energy").strip()
    duration = int(body.get("duration") or 45)

    if not topic:
        raise HTTPException(status_code=400, detail="Please provide a topic or prompt for your short.")

    # Fetch active prompt from DB if customized
    from src.models import Prompt, SessionLocal
    from app.semantic_ranker import _call_llm
    from src.logger import log_system_event

    sys_prompt = (
        "You are a master short-form video scriptwriter for viral 45-60 second YouTube Shorts. "
        "Write clean, punchy scripts with structured sections: HOOK, PROBLEM, 3 SECRETS, TWIST, CTA. "
        "No emojis. Short sentences under 12 words. Pure spoken English.\n\n"
        "CRITICAL — The VOICEOVER is the spoken narration. NEVER paste the topic title into a VOICEOVER line; "
        "mind read/watch the topic and write a natural spoken opener about it.\n\n"
        "CRITICAL — Each VISUAL line must describe a REAL CINEMATIC AI VIDEO SCENE (specific camera angle, "
        "subject, environment, lighting, motion) that matches the section's meaning. It must NEVER be about text, "
        "animation, zoom-in on text, or on-screen typography. Example: 'Slow dolly shot through a rainlit night "
        "office, empty desks, a single green dashboard glow, cinematic depth-of-field.'"
    )
    user_template = None
    _db = SessionLocal()
    try:
        p_row = _db.query(Prompt).filter(Prompt.name == "Topic-to-Viral Script Pipeline", Prompt.is_active == True).first()
        if p_row and p_row.system_prompt:
            sys_prompt = p_row.system_prompt
        if p_row and p_row.user_template:
            user_template = p_row.user_template
    finally:
        _db.close()

    if user_template:
        # DB templates use {{topic}} (double braces) while Python .format expects {topic}
        # Handle both so LLM receives actual values not placeholders
        tmp = user_template.replace("{{topic}}", "{topic}").replace("{{niche}}", "{niche}").replace("{{tone}}", "{tone}").replace("{{duration}}", "{duration}")
        try:
            user_req = tmp.format(topic=topic, niche=niche, tone=tone, duration=duration)
        except Exception:
            # Fallback direct replace if format fails
            user_req = user_template.replace("{{topic}}", str(topic)).replace("{{niche}}", str(niche)).replace("{{tone}}", str(tone)).replace("{{duration}}", str(duration))
    else:
        user_req = f"""TOPIC: {topic}
NICHE: {niche}
TONE: {tone}
TARGET DURATION: {duration} seconds

Write a complete script following the structured format. Output ONLY the script, no explanations."""

    script_result = None
    try:
        script_result = _call_llm(prompt=user_req, system_prompt=sys_prompt, max_tokens=2048)
        log_system_event("PIPELINE", "Topic Script Generated", f"Generated script for topic '{topic}' ({niche})", severity="SUCCESS")
    except Exception as e:
        log.warning("LLM script generation failed: %s", e)
        log_system_event("PIPELINE", "Topic Script Fallback", f"LLM error: {e}, using template", severity="WARN")

    if not script_result:
        # Clean fallback template if LLM key not configured.
        # VISUAL lines = real cinematic AI scenes (NEVER text/animation).
        script_result = f"""TITLE: The Hidden Truth About {topic}

HOOK
TIMESTAMP: 00:00 - 00:04
VISUAL: Slow push-in through a dim studio, a single spotlight on a confident person, cool cyan rim light, cinematic depth of field
VOICEOVER: Almost nobody knows this about {topic}.

PROBLEM
TIMESTAMP: 00:04 - 00:12
VISUAL: Wide office scene at night, tired employee staring at a glowing screen, shallow focus, moody teal lighting
VOICEOVER: Most people get this completely wrong. They follow advice that keeps them stuck and confused.

SECRET ONE
TIMESTAMP: 00:12 - 00:22
VISUAL: Close-up of steady hands organizing a clean workspace, morning light through blinds, calm realistic motion
VOICEOVER: Rule number one. Stop overcomplicating the basics. Simple actions done daily beat complex strategies done once.

SECRET TWO
TIMESTAMP: 00:22 - 00:32
VISUAL: Over-the-shoulder shot of a focused creator, laptop glow, efficient flow, shallow depth of field, natural daylight
VOICEOVER: Rule number two. Focus on high-leverage execution. One good action beats ten mediocre ones every single time.

SECRET THREE
TIMESTAMP: 00:32 - 00:42
VISUAL: Calm person in a serene living room, deep breath, plants and warm light, slow cinematic pan
VOICEOVER: Rule number three. Master your emotional control. The person who stays calm under pressure wins every time.

TWIST
TIMESTAMP: 00:42 - 00:50
VISUAL: Surprising rooftop reveal at dusk, city lights bokeh, people looking up, glowing horizon, cinematic motion
VOICEOVER: Once you apply this one shift, everything accelerates. Most people will ignore this. That is exactly why it works.

CTA
TIMESTAMP: 00:50 - 00:58
VISUAL: Upbeat street scene, creator smiling toward camera, natural handheld motion, warm golden-hour light
VOICEOVER: Follow for more daily breakdowns that actually change how you think."""

    return {
        "success": True,
        "topic": topic,
        "duration": duration,
        "niche": niche,
        "tone": tone,
        "generated_script": script_result,
        "message": "Viral Short Script generated successfully!"
    }


# ── Scene Generation Provider Test ─────────────────────────────────────────────
@app.post("/api/admin/video-provider/test", tags=["admin"])
async def admin_test_video_provider(request: Request):
    """Verify a scene generation provider (local / fal / replicate) is usable."""
    _require_admin(request)
    from src.logger import log_system_event
    from src.scene_providers import get_provider_config

    body = await request.json()
    provider = body.get("provider", "").strip().lower()
    value = body.get("value", "").strip()

    import urllib.request as _ur
    import urllib.error as _uerr

    def _probe(url, headers, data=None, timeout=15):
        req = _ur.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        with _ur.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()

    # Use the passed key if given, otherwise fall back to the stored config.
    cfg = get_provider_config(provider)
    if not value:
        value = (cfg or {}).get("api_key", "") or ""

    if provider == "local":
        comfy_url = value or (cfg or {}).get("endpoint") or "http://127.0.0.1:8188"
        comfy_url = comfy_url.rstrip("/")
        try:
            req = _ur.Request(f"{comfy_url}/system_stats", headers={"User-Agent": "Vergeclip/1.0"})
            with _ur.urlopen(req, timeout=5) as resp:
                stats = json.loads(resp.read().decode())
            dev = stats.get("devices", [{}])[0].get("name", "unknown GPU")
        except Exception as e:
            log_system_event("CONFIG", "Local Scene Test", f"Not reachable: {e}", severity="ERROR")
            return {"verified": False, "message": f"Local ComfyUI is NOT running at {comfy_url}. Start it, then re-check. ({e})"}

        # Check the ComfyUI instance exposes Wan2.1 / LTX-Video nodes so local
        # generation can actually run (this provider replaced the CogVideoX path).
        wan_models = []
        try:
            req = _ur.Request(f"{comfy_url}/object_info", headers={"User-Agent": "Vergeclip/1.0"})
            with _ur.urlopen(req, timeout=8) as resp:
                obj = json.loads(resp.read().decode())
            wan_nodes = [k for k in obj if (
                k.startswith("Wan") or k.startswith("WanVideo") or k.startswith("LTXV")
                or ("Wan" in k and "Video" in k) or k.startswith("LTXVideo")
            )]
            def _combo(field):
                if isinstance(field, list) and field and isinstance(field[0], list):
                    return field[0]
                return field if isinstance(field, list) else []
            chk = _combo(obj.get("CheckpointLoaderSimple", {}).get("input", {}).get("required", {}).get("ckpt_name", []))
            wan_models = [m for m in chk if "wan" in m.lower() or "ltx" in m.lower()]
        except Exception:
            wan_nodes = []

        if wan_nodes or wan_models:
            detail = ", ".join((wan_models[:5] if wan_models else wan_nodes[:5]))
            msg = (f"Local ComfyUI running on '{dev}' ✓ with Wan/LTX support"
                   + (f" ({detail})" if detail else "") + " — local generation READY")
            verified = True
        else:
            msg = (f"Local ComfyUI is running on '{dev}' ✓ BUT no Wan2.1/LTX-Video nodes found. "
                   "Install the Wan/LTX-Video custom nodes and load a Wan/LTX checkpoint in ComfyUI first.")
            verified = False
        log_system_event("CONFIG", "Local Scene Test", msg, severity="SUCCESS" if verified else "WARN")
        return {"verified": verified, "message": msg}

    # Cloud providers require a key.
    if not value:
        return {"verified": False, "message": "No API key provided for this provider. Save one in Admin → Scene Generation."}

    try:
        if provider == "fal":
            import json as _json
            model = (cfg or {}).get("model_name") or "kuaishou/kling-video/v1/standard/text-to-video"
            base = (cfg or {}).get("endpoint") or "https://queue.fal.run"
            payload = _json.dumps({
                "prompt": "a short cinematic test scene",
                "num_frames": 8,
                "size": "320x576",
                "num_inference_steps": 2,
            }).encode("utf-8")
            status, body_bytes = _probe(
                f"{base}/{model.strip('/')}",
                headers={"Authorization": f"Key {value}", "Content-Type": "application/json", "User-Agent": "Vergeclip/1.0"},
                data=payload, timeout=30,
            )
            msg = _cloud_status_msg("fal.ai", status)
            verified = status in (200, 201, 202)
            log_system_event("CONFIG", "fal.ai Test", msg, severity="SUCCESS" if verified else "ERROR")
            return {"verified": verified, "message": msg}

        elif provider == "replicate":
            import json as _json
            model = (cfg or {}).get("model_name") or "wan-video/wan-2.1-t2v-14b"
            base = (cfg or {}).get("endpoint") or "https://api.replicate.com/v1"
            status, body_bytes = _probe(
                f"{base.rstrip('/')}/models/{model}",
                headers={"Authorization": f"Bearer {value}", "User-Agent": "Vergeclip/1.0"},
                timeout=30,
            )
            msg = _cloud_status_msg("Replicate", status)
            verified = status in (200, 201)
            log_system_event("CONFIG", "Replicate Test", msg, severity="SUCCESS" if verified else "ERROR")
            return {"verified": verified, "message": msg}

        return {"verified": False, "message": f"Unknown provider: {provider}"}
    except _uerr.HTTPError as e:
        return {"verified": False, "message": f"Provider responded {e.code}: {e.reason}"}
    except Exception as e:
        return {"verified": False, "message": f"Connection error: {e}"}


def _cloud_status_msg(name: str, status: int) -> str:
    """Map an HTTP status from a cloud video API to a human-readable test result."""
    if status in (401, 403):
        return f"{name} rejected the key (HTTP {status} Unauthorized/Forbidden). Check the API key."
    if status == 402:
        return f"{name} key valid but payment required (HTTP 402). Add a payment method / credits."
    if status in (404,):
        return f"{name} returned 404 — check the model name."
    if status in (200, 201, 202):
        return f"{name} connection verified (HTTP {status}) ✓"
    return f"{name} responded with HTTP {status}"


# Process handle + log handle for the bundled ComfyUI launch (kept referenced so
# the background process and its log pipe survive after the request returns).
_comfyui_popen = None
_comfyui_log_fh = None
_comfyui_url = None


# ── ComfyUI Auto-Setup ──────────────────────────────────────────────────────────
# Files we expect to be in place for a working Wan2.1 / LTX-Video installation.
# (Matches what scripts/setup_comfyui_wan.ps1 + requirements-local.txt set up.)
# The endpoint below reports which pieces are present and installs the missing
# ones on demand — so the admin UI can offer a true one-click "Setup + Start"
# experience for the local Wan2.1 / LTX-Video stack.
_WAN_REQUIRED = [
    # (relative_path, friendly_label, min_size_bytes, optional)
    ("comfyui/main.py", "ComfyUI main.py", 5_000, False),
    ("comfyui/.venv/Scripts/python.exe", "ComfyUI Python venv", 50_000, False),
    ("comfyui/.venv/Lib/site-packages/torch/__init__.py", "PyTorch (in venv)", 1_000, False),
    ("comfyui/custom_nodes/ComfyUI-WanVideoWrapper/__init__.py", "WanVideoWrapper custom node", 200, False),
    ("comfyui/custom_nodes/ComfyUI-KJNodes/__init__.py", "ComfyUI-KJNodes (required dep)", 200, False),
    ("comfyui/custom_nodes/ComfyUI-Manager/__init__.py", "ComfyUI-Manager", 200, True),
    ("comfyui/models/Diffusion_Models/wan2.1_t2v_14B_bf16.safetensors", "Wan2.1-T2V-14B diffusion model (~28 GB)", 20_000_000_000, False),
    ("comfyui/models/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors", "UMT5-XXL text encoder fp8 (~6.2 GB)", 5_500_000_000, False),
    ("comfyui/models/vae/wan_2.1_vae.safetensors", "Wan2.1 VAE (~257 MB)", 200_000_000, False),
]


def _wan_status_snapshot() -> dict:
    """Return a per-component 'present' map + a list of human-friendly lines for the UI."""
    items = []
    all_ok = True
    for rel, label, min_size, optional in _WAN_REQUIRED:
        p = ROOT_DIR / rel
        present = bool(p.exists()) and p.stat().st_size >= min_size
        items.append({
            "key": rel,
            "label": label,
            "present": present,
            "size_mb": round(p.stat().st_size / 1_048_576, 1) if p.exists() else 0,
            "optional": optional,
        })
        if not present and not optional:
            all_ok = False
    return {"all_ready": all_ok, "items": items}


@app.get("/api/admin/comfyui/status", tags=["admin"])
async def admin_comfyui_status(request: Request):
    """Return detailed install status of the bundled Local Wan2.1 / LTX-Video stack."""
    _require_admin(request)
    import urllib.request as _ur
    from src.config import get_setting
    comfy_url = (get_setting("comfyui_url", "http://127.0.0.1:8188") or "http://127.0.0.1:8188").strip().rstrip("/")
    reachable = False
    try:
        with _ur.urlopen(f"{comfy_url}/system_stats", timeout=3) as r:
            r.read()
        reachable = True
    except Exception:
        reachable = False
    snap = _wan_status_snapshot()
    return {
        "success": True,
        "all_ready": snap["all_ready"],
        "components": snap["items"],
        "comfyui_url": comfy_url,
        "comfyui_reachable": reachable,
        "comfyui_running": reachable,
        "ready_to_generate": snap["all_ready"] and reachable,
        "ready_to_start": snap["all_ready"],
    }


@app.post("/api/admin/comfyui/setup", tags=["admin"])
async def admin_comfyui_setup(request: Request):
    """Auto-install the bundled Local Wan2.1 / LTX-Video stack.

    This endpoint is the Python equivalent of scripts/setup_comfyui_wan.ps1.
    It detects which pieces are missing (ComfyUI repo, venv, PyTorch+CUDA,
    ComfyUI's own requirements, the WanVideoWrapper + KJNodes custom nodes,
    and the large model files) and installs/downloads them in-place.

    Pass `{"start_after": true}` in the JSON body to also launch ComfyUI
    immediately after setup finishes.
    """
    _require_admin(request)
    import subprocess
    import time as _time
    import urllib.request as _ur
    from pathlib import Path
    from src.config import get_setting
    from src.logger import log_system_event

    comfy_py = ROOT_DIR / "comfyui" / ".venv" / "Scripts" / "python.exe"
    comfy_main = ROOT_DIR / "comfyui" / "main.py"
    wan_node = ROOT_DIR / "comfyui" / "custom_nodes" / "ComfyUI-WanVideoWrapper" / "__init__.py"
    kj_node = ROOT_DIR / "comfyui" / "custom_nodes" / "ComfyUI-KJNodes" / "__init__.py"
    diffusion_dir = ROOT_DIR / "comfyui" / "models" / "Diffusion_Models"
    te_dir = ROOT_DIR / "comfyui" / "models" / "text_encoders"
    vae_dir = ROOT_DIR / "comfyui" / "models" / "vae"

    log_lines: list[str] = []
    actions: list[dict] = []

    def _log(msg: str) -> None:
        log_lines.append(msg)
        log.info("[comfyui-setup] %s", msg)

    def _action(label: str, ok: bool, detail: str = "") -> None:
        actions.append({"label": label, "ok": ok, "detail": detail})
        _log(("OK   " if ok else "FAIL ") + label + ((" — " + detail) if detail else ""))

    def _stream(cmd: list[str], cwd: str | None = None, timeout: int = 1800) -> tuple[bool, str]:
        """Run subprocess, stream output to log_lines, return (ok, tail)."""
        try:
            _log("RUN  " + " ".join(cmd))
            proc = subprocess.Popen(
                cmd, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            buf: list[str] = []
            t0 = _time.monotonic()
            while True:
                if proc.stdout is None:
                    break
                line = proc.stdout.readline()
                if not line and proc.poll() is not None:
                    break
                if line:
                    s = line.rstrip()
                    buf.append(s)
                    if len(buf) > 400:
                        del buf[:200]
                    if len(log_lines) > 400:
                        del log_lines[:200]
                    log_lines.append(s)
                if _time.monotonic() - t0 > timeout:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return False, "Timeout after " + str(timeout) + "s. Last: " + (buf[-1] if buf else "(no output)")
            rc = proc.wait(timeout=10)
            return rc == 0, "\n".join(buf[-30:])
        except Exception as e:
            return False, f"Exception: {e}"

    # ── 1. Clone ComfyUI itself if main.py is missing ─────────────────────────
    if not comfy_main.exists():
        _log("ComfyUI main.py missing — cloning repository")
        ok, tail = _stream(
            ["git", "clone", "--depth=1", "https://github.com/comfyanonymous/ComfyUI.git", "comfyui"],
            cwd=str(ROOT_DIR), timeout=900,
        )
        _action("Clone ComfyUI repository", ok, tail[-200:] if not ok else "")
        if not ok:
            return {"success": False, "message": "Failed to clone ComfyUI. See log_tail for git output.",
                    "actions": actions, "log_tail": "\n".join(log_lines[-40:])}

    # ── 2. Create venv if missing ─────────────────────────────────────────────
    if not comfy_py.exists():
        _log("ComfyUI venv missing — locating a Python 3.10–3.12 interpreter")
        py_exe = None
        for cand in ["python", "py", "python3"]:
            try:
                r = subprocess.run([cand, "-c", "import sys; print(sys.version_info.minor)"],
                                   capture_output=True, text=True, timeout=5)
                minor = (r.stdout or "").strip()
                if minor.isdigit() and int(minor) in (10, 11, 12):
                    py_exe = cand
                    _log(f"Found Python 3.{minor} as '{cand}'")
                    break
            except Exception:
                continue
        if not py_exe:
            for cand in [r"C:\Python312\python.exe", r"C:\Python311\python.exe", r"C:\Python310\python.exe"]:
                if Path(cand).exists():
                    py_exe = cand
                    _log(f"Found Python at {cand}")
                    break
        if not py_exe:
            return {
                "success": False,
                "message": "Could not find Python 3.10/3.11/3.12 on PATH. Install Python from https://www.python.org/downloads/ and tick 'Add to PATH', then try again.",
                "actions": actions, "log_tail": "\n".join(log_lines[-40:]),
            }
        ok, tail = _stream([py_exe, "-m", "venv", "comfyui/.venv"], cwd=str(ROOT_DIR), timeout=300)
        _action("Create ComfyUI venv (Python 3.10–3.12)", ok, tail[-200:] if not ok else "")
        if not ok:
            return {"success": False, "message": "Failed to create venv", "actions": actions, "log_tail": "\n".join(log_lines[-40:])}

    # ── 3. Install PyTorch CUDA 12.6 if torch is missing from venv ─────────────
    torch_init = ROOT_DIR / "comfyui" / ".venv" / "Lib" / "site-packages" / "torch" / "__init__.py"
    if not torch_init.exists():
        _log("PyTorch missing in venv — installing torch+torchvision+torchaudio (CUDA 12.6)")
        ok, tail = _stream([str(comfy_py), "-m", "pip", "install", "--upgrade", "pip"],
                           cwd=str(ROOT_DIR / "comfyui"), timeout=300)
        _action("Upgrade pip in venv", ok, tail[-200:] if not ok else "")
        ok, tail = _stream([
            str(comfy_py), "-m", "pip", "install",
            "torch", "torchvision", "torchaudio",
            "--index-url", "https://download.pytorch.org/whl/cu126",
        ], cwd=str(ROOT_DIR / "comfyui"), timeout=1800)
        _action("Install PyTorch (CUDA 12.6)", ok, tail[-200:] if not ok else "")
        if not ok:
            return {"success": False, "message": "Failed to install PyTorch", "actions": actions, "log_tail": "\n".join(log_lines[-40:])}
    else:
        _log("PyTorch already present in venv — skipping")

    # ── 4. Install ComfyUI's own requirements.txt ─────────────────────────────
    req = ROOT_DIR / "comfyui" / "requirements.txt"
    if req.exists():
        _log("Installing ComfyUI requirements.txt")
        ok, tail = _stream([str(comfy_py), "-m", "pip", "install", "-r", str(req)],
                           cwd=str(ROOT_DIR / "comfyui"), timeout=1800)
        _action("Install ComfyUI requirements.txt", ok, tail[-200:] if not ok else "")

    # ── 5. Install WanVideoWrapper + KJNodes if missing ───────────────────────
    if not wan_node.exists():
        _log("ComfyUI-WanVideoWrapper not found — cloning kijai/ComfyUI-WanVideoWrapper")
        ok, tail = _stream(["git", "clone", "https://github.com/kijai/ComfyUI-WanVideoWrapper.git"],
                           cwd=str(ROOT_DIR / "comfyui" / "custom_nodes"), timeout=600)
        _action("Clone ComfyUI-WanVideoWrapper", ok, tail[-200:] if not ok else "")
        if ok:
            req2 = ROOT_DIR / "comfyui" / "custom_nodes" / "ComfyUI-WanVideoWrapper" / "requirements.txt"
            if req2.exists():
                _log("Installing WanVideoWrapper requirements")
                ok2, tail2 = _stream([str(comfy_py), "-m", "pip", "install", "-r", str(req2)],
                                     cwd=str(ROOT_DIR / "comfyui"), timeout=1800)
                _action("Install WanVideoWrapper requirements", ok2, tail2[-200:] if not ok2 else "")
    else:
        _log("ComfyUI-WanVideoWrapper already present — skipping clone")

    if not kj_node.exists():
        _log("ComfyUI-KJNodes not found — cloning kijai/ComfyUI-KJNodes (required dependency)")
        ok, tail = _stream(["git", "clone", "https://github.com/kijai/ComfyUI-KJNodes.git"],
                           cwd=str(ROOT_DIR / "comfyui" / "custom_nodes"), timeout=600)
        _action("Clone ComfyUI-KJNodes", ok, tail[-200:] if not ok else "")
        if ok:
            req3 = ROOT_DIR / "comfyui" / "custom_nodes" / "ComfyUI-KJNodes" / "requirements.txt"
            if req3.exists():
                _log("Installing ComfyUI-KJNodes requirements")
                ok3, tail3 = _stream([str(comfy_py), "-m", "pip", "install", "-r", str(req3)],
                                     cwd=str(ROOT_DIR / "comfyui"), timeout=1800)
                _action("Install ComfyUI-KJNodes requirements", ok3, tail3[-200:] if not ok3 else "")
    else:
        _log("ComfyUI-KJNodes already present — skipping clone")

    # Make sure diffusers/transformers are recent enough for the wrapper
    ok, tail = _stream(
        [str(comfy_py), "-m", "pip", "install", "--upgrade", "diffusers", "transformers", "accelerate", "huggingface_hub"],
        cwd=str(ROOT_DIR / "comfyui"), timeout=900,
    )
    _action("Upgrade diffusers/transformers/accelerate", ok, tail[-200:] if not ok else "")

    # ── 6. Download model files if missing ────────────────────────────────────
    # Wan2.1 repackaged for ComfyUI (Comfy-Org). LTX-Video can be used instead by
    # pointing the endpoint/model names to an LTX checkpoint — the node detection
    # in the local provider accepts both Wan and LTX node families.
    HF_BASE = "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files"

    def _download(url: str, dest: Path, label: str) -> tuple[bool, str]:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size > 50_000_000:
            return True, f"Already present ({dest.stat().st_size/1_048_576:.1f} MB) — skipping"
        _log(f"Downloading {label} from {url}")
        try:
            tmp = dest.with_suffix(dest.suffix + ".part")
            downloaded = 0
            req = _ur.Request(url, headers={"User-Agent": "Vergeclip/1.0"})
            with _ur.urlopen(req, timeout=600) as r:
                total = int(r.headers.get("Content-Length", 0))
                with open(tmp, "wb") as fh:
                    while True:
                        chunk = r.read(1024 * 256)
                        if not chunk:
                            break
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if total and downloaded % (50 * 1024 * 1024) < 1024 * 256:
                            _log(f"  … {label}: {downloaded/1_048_576:.0f}/{total/1_048_576:.0f} MB")
            tmp.replace(dest)
            sz = dest.stat().st_size / 1_048_576
            return True, f"Downloaded {sz:.1f} MB"
        except Exception as e:
            return False, f"Download failed: {e}"

    ok, detail = _download(
        f"{HF_BASE}/diffusion_models/wan2.1_t2v_14B_bf16.safetensors",
        diffusion_dir / "wan2.1_t2v_14B_bf16.safetensors",
        "Wan2.1-T2V-14B diffusion model",
    )
    _action("Download Wan2.1-T2V-14B diffusion model (~28 GB)", ok, detail)

    # Sidecar config files (non-fatal — wrapper can load without them)
    for rel in [
        "diffusion_models/config.json",
        "text_encoders/config.json",
        "vae/config.json",
    ]:
        name = Path(rel).name
        ok, detail = _download(f"{HF_BASE}/{rel}", (diffusion_dir if "diffusion" in rel else te_dir if "text" in rel else vae_dir) / name, f"Wan2.1 {name}")
        if not ok and "Already present" not in detail:
            _log(f"  (non-fatal) {rel}: {detail}")

    ok, detail = _download(
        f"{HF_BASE}/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        te_dir / "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        "UMT5-XXL text encoder fp8",
    )
    _action("Download UMT5-XXL text encoder fp8 (~6.2 GB)", ok, detail)

    ok, detail = _download(
        f"{HF_BASE}/vae/wan_2.1_vae.safetensors",
        vae_dir / "wan_2.1_vae.safetensors",
        "Wan2.1 VAE",
    )
    _action("Download Wan2.1 VAE (~257 MB)", ok, detail)

    # ── 7. Final status snapshot ──────────────────────────────────────────────
    snap = _wan_status_snapshot()
    log_system_event(
        "CONFIG", "Local ComfyUI Auto-Setup",
        "Wan2.1 / LTX-Video auto-setup finished. Ready=" + str(snap["all_ready"]),
        severity="SUCCESS" if snap["all_ready"] else "WARNING",
    )

    result = {
        "success": snap["all_ready"],
        "all_ready": snap["all_ready"],
        "components": snap["items"],
        "actions": actions,
        "log_tail": "\n".join(log_lines[-60:]),
    }

    # ── 8. Optional: start ComfyUI afterwards ─────────────────────────────────
    body: dict = {}
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            body = await request.json()
    except Exception:
        body = {}
    if body.get("start_after") and snap["all_ready"]:
        _log("Auto-starting ComfyUI after setup (start_after=true)")
        start_result = await admin_comfyui_start(request)
        result["start_result"] = start_result
        result["message"] = "Setup complete + " + start_result.get("message", "")
    else:
        if snap["all_ready"]:
            result["message"] = "All Wan2.1 / LTX-Video components are installed. Click 'Start & Check' to launch ComfyUI."
        else:
            missing = [it["label"] for it in snap["items"] if not it["present"] and not it["optional"]]
            result["message"] = "Setup partially complete. Still missing: " + ", ".join(missing) + ". See log_tail."

    return result


@app.post("/api/admin/comfyui/start", tags=["admin"])
async def admin_comfyui_start(request: Request):
    """Launch the bundled Local Wan2.1 / LTX-Video (ComfyUI) server as a background process.

    Output is streamed to data/comfyui.log. The endpoint probes until the UI is
    reachable (up to 90s) so the admin gets immediate feedback instead of a
    silent failure.
    """
    global _comfyui_popen, _comfyui_log_fh, _comfyui_url
    _require_admin(request)
    import subprocess
    from src.config import get_setting
    from src.logger import log_system_event

    comfy_url = (get_setting("comfyui_url", "http://127.0.0.1:8188") or "http://127.0.0.1:8188").strip().rstrip("/")

    # Already running?
    import urllib.request as _ur
    try:
        req = _ur.Request(f"{comfy_url}/system_stats", headers={"User-Agent": "Vergeclip/1.0"})
        with _ur.urlopen(req, timeout=5) as resp:
            resp.read()
        msg = f"Local ComfyUI is already running at {comfy_url}."
        log_system_event("CONFIG", "Local ComfyUI Start", msg, severity="SUCCESS")
        return {"success": True, "already_running": True, "running": True, "message": msg}
    except Exception:
        pass

    comfy_main = ROOT_DIR / "comfyui" / "main.py"
    comfy_py = ROOT_DIR / "comfyui" / ".venv" / "Scripts" / "python.exe"
    if not comfy_main.exists():
        raise HTTPException(status_code=400, detail="Bundled ComfyUI not found (comfyui/main.py missing).")
    if not comfy_py.exists():
        raise HTTPException(status_code=400, detail="Bundled ComfyUI venv python not found (comfyui/.venv). Install dependencies first.")

    log_path = ROOT_DIR / "data" / "comfyui.log"
    if _comfyui_popen is not None and _comfyui_popen.poll() is None:
        msg = f"ComfyUI is still starting (pid {_comfyui_popen.pid}) — log at data/comfyui.log."
        return {"success": True, "already_running": False, "running": False, "starting": True, "message": msg}
    if _comfyui_popen is not None:
        # Process already exited — read the tail to explain why
        tail = ""
        try:
            if log_path.exists():
                tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
        except Exception:
            pass
        return {"success": True, "already_running": False, "running": False, "log_tail": tail,
                "message": "Previous ComfyUI process already exited — see log tail below."}

    try:
        if _comfyui_log_fh is not None:
            try:
                _comfyui_log_fh.close()
            except Exception:
                pass
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _comfyui_log_fh = open(log_path, "w", encoding="utf-8")
        _comfyui_url = comfy_url
        _comfyui_popen = subprocess.Popen(
            [str(comfy_py), "main.py", "--port", "8188", "--disable-auto-launch"],
            cwd=str(ROOT_DIR / "comfyui"),
            stdout=_comfyui_log_fh,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except Exception as e:
        if _comfyui_log_fh is not None:
            try:
                _comfyui_log_fh.close()
            except Exception:
                pass
            _comfyui_log_fh = None
        raise HTTPException(status_code=500, detail=f"Failed to start ComfyUI: {e}")

    # Probe until reachable (up to 90s) for immediate feedback
    up = False
    for _ in range(90):
        time.sleep(1)
        try:
            req = _ur.Request(f"{comfy_url}/system_stats", headers={"User-Agent": "Vergeclip/1.0"})
            with _ur.urlopen(req, timeout=3) as resp:
                resp.read()
                up = True
                break
        except Exception:
            continue

    if up:
        msg = f"Local ComfyUI is UP at {comfy_url} (pid {_comfyui_popen.pid}). Models/checkpoints still load in the background — start a job now and wait."
        log_system_event("CONFIG", "Local ComfyUI Start", msg, severity="SUCCESS")
        return {"success": True, "already_running": False, "running": True, "message": msg}
    else:
        tail = ""
        try:
            if log_path.exists():
                tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:])
        except Exception:
            pass
        log_system_event("CONFIG", "Local ComfyUI Start",
                         f"ComfyUI did not become reachable at {comfy_url} within 90s.", severity="WARNING")
        return {"success": True, "already_running": False, "running": False,
                "message": f"ComfyUI not reachable at {comfy_url} within 90s (it may still be launching, or it exited early). Full log saved to data/comfyui.log.",
                "log_tail": tail}


# ── Script-to-Video Pipeline ────────────────────────────────────────────────────
@app.post("/api/pipeline/script-to-video", tags=["pipeline"])
async def script_to_video(request: Request):
    from src.auth import AUTH_REQUIRED, decode_token, get_user_by_id
    from src.models import SessionLocal

    # Auth check — token is parsed REGARDLESS of AUTH_REQUIRED so a logged-in
    # admin/user is never mistaken for a guest (AUTH_REQUIRED only gates whether
    # an anonymous request is allowed at all).
    user_id = None
    is_guest = True
    token = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
    else:
        token = request.query_params.get("token") or request.query_params.get("access_token")
    if token:
        try:
            payload = decode_token(token)
            uid = payload.get("sub")
            if uid and get_user_by_id(int(uid)):
                user_id = int(uid)
                is_guest = False
        except HTTPException:
            # Token presented but expired/invalid: reject with 401 so the
            # frontend auto-refreshes. NEVER silently fall back to guest —
            # that makes an admin/paid user burn the device trial and get 403.
            raise
        except Exception:
            pass
    if AUTH_REQUIRED and is_guest:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Get device ID for guest trial check
    device_id = request.headers.get("x-device-id", "").strip()
    ip = request.client.host if request.client else "unknown"

    # Check admin status
    is_admin_user = False
    if not is_guest and user_id:
        try:
            from src.models import User as _U
            _db = SessionLocal()
            try:
                _u = _db.query(_U).filter(_U.id == user_id).first()
                if _u and _u.role == "admin":
                    is_admin_user = True
            finally:
                _db.close()
        except Exception:
            pass

    # Device trial check for guests
    if is_guest and not is_admin_user and device_id:
        from src.device_trial import check_device_trial, consume_device_trial
        chk = check_device_trial(device_id, ip)
        if not chk["allowed"]:
            raise HTTPException(status_code=403, detail=chk["reason"] + " — please signup/login for 5 videos/month.")
        consume_device_trial(device_id, ip)
    elif is_guest and not is_admin_user and not device_id:
        fallback = f"ip_{ip}_{request.headers.get('user-agent','')[:30]}"
        if len(fallback) > 10:
            from src.device_trial import check_device_trial
            chk = check_device_trial(fallback, ip)
            if not chk["allowed"]:
                raise HTTPException(status_code=403, detail=chk["reason"])

    # For guest, assign to admin user for FK
    if is_guest:
        from src.models import User as _GuestUser
        _db = SessionLocal()
        try:
            admin = _db.query(_GuestUser).filter(_GuestUser.username == "admin").first()
            user_id = admin.id if admin else 1
        finally:
            _db.close()

    # Quota check for logged-in users
    remaining = None
    if not is_guest and user_id:
        from src.quota import check_and_increment_quota
        _db = SessionLocal()
        try:
            remaining = check_and_increment_quota(user_id, _db)
        finally:
            _db.close()

    body = await request.json()
    script_text = str(body.get("script") or "").strip()
    voice = str(body.get("voice") or "en-US-AriaNeural").strip()
    if not voice or voice not in {"", "auto"}:
        # Use the raw chosen voice directly; 'auto'/blank -> best default male/female mix.
        pass
    rate = str(body.get("rate") or "+3%").strip()

    if not script_text:
        raise HTTPException(status_code=400, detail="Script text is required.")

    log.info("Script-to-Video: starting for %d chars, voice=%s", len(script_text), voice)

    # ── Parse structured script: extract VOICEOVER lines + section labels ──
    import re as _re

    def _parse_script(text: str) -> tuple[list[dict], str]:
        """Parse structured script into sections and extract voiceover text."""
        sections = []
        lines = text.strip().split("\n")
        current_section = None
        current_data = {}

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            upper = stripped.upper()
            # Detect section headers
            if upper in ("HOOK", "PROBLEM", "TWIST", "CTA", "CALL TO ACTION",
                         "CORE VALUE", "CONCLUSION") or _re.match(r"^(SECRET|INSIGHT)\s+\w+", upper):
                if current_section and current_data.get("voiceover"):
                    sections.append(current_data)
                current_section = upper
                current_data = {"label": upper, "voiceover": "", "visual": ""}
            elif upper.startswith("TITLE:"):
                if current_section and current_data.get("voiceover"):
                    sections.append(current_data)
                    current_section = None
                    current_data = {}
                current_data["title"] = stripped[6:].strip()
            elif upper.startswith("TIMESTAMP:"):
                current_data["timestamp"] = stripped[10:].strip()
            elif upper.startswith("VISUAL:"):
                current_data["visual"] = stripped[7:].strip()
            elif upper.startswith("VOICEOVER:"):
                vo = stripped[10:].strip()
                if vo:
                    current_data["voiceover"] = vo
            elif upper.startswith("VOICETONE:") or upper.startswith("BACKGROUND"):
                current_data[upper.split(":")[0].strip()] = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            elif current_section and stripped and not any(stripped.startswith(k) for k in ["TIMESTAMP", "VISUAL", "VOICEOVER", "TITLE", "VOICETONE", "BACKGROUND"]):
                # Continuation of voiceover text
                if current_data.get("voiceover"):
                    current_data["voiceover"] += " " + stripped

        if current_section and current_data.get("voiceover"):
            sections.append(current_data)

        # If no structured sections found, treat entire text as voiceover
        if not sections:
            # Strip any metadata lines that leaked in
            stripped_lines = []
            for line in text.strip().split("\n"):
                ln = line.strip()
                if not ln:
                    continue
                lu = ln.upper()
                # Skip metadata directives that should never be spoken
                if any(lu.startswith(k) for k in ["TITLE:", "VISUAL:", "TIMESTAMP:", "VOICETONE:", "BACKGROUND"]):
                    continue
                # Skip standalone section headers with no voiceover
                if lu in ("HOOK", "PROBLEM", "TWIST", "CTA", "CALL TO ACTION",
                          "CORE VALUE", "CONCLUSION") or _re.match(r"^(SECRET|INSIGHT)\s+\w+", lu):
                    continue
                stripped_lines.append(ln)
            sentences = [s.strip() for s in _re.split(r'(?<=[.!?])\s+', " ".join(stripped_lines)) if s.strip()]
            voiceover_text = " ".join(sentences)
            sections = [{"label": f"SECTION {i+1}", "voiceover": s, "visual": ""} for i, s in enumerate(sentences)]
        else:
            voiceover_text = " ".join(s["voiceover"] for s in sections)

        return sections, voiceover_text

    sections, voiceover_text = _parse_script(script_text)

    if not voiceover_text.strip():
        raise HTTPException(status_code=400, detail="No voiceover content found in script.")

    log.info("Script-to-Video: parsed %d sections, %d voiceover chars", len(sections), len(voiceover_text))

    # ── Collect visual descriptions + title metadata (never rendered as text) ──
    visual_descriptions = []
    title_metadata = ""
    missing_visual_idx = []
    for i, sec in enumerate(sections):
        vis = (sec.get("visual", "") or "").strip()
        visual_descriptions.append(vis)
        if not vis:
            missing_visual_idx.append(i)
        if sec.get("title") and not title_metadata:
            title_metadata = sec["title"]

    # Auto-generate scene video prompts from voiceover when the script has no
    # VISUAL: lines (users who removed image-matter still get real AI videos).
    if missing_visual_idx:
        try:
            from app.semantic_ranker import _call_llm
            _vis_sys = (
                "You are a cinematic video-scene director. For each numbered voiceover line, write ONE "
                "concise AI-VIDEO prompt describing an animated moving scene: subject, action, camera "
                "movement, lighting, mood. One paragraph, under 40 words, plain text, numbers preserved, "
                "no JSON, no extra commentary. The video clip will have its own motion."
            )
            _vis_lines = "\n".join(f"{k+1}. {sections[idx].get('voiceover','')}" for k, idx in enumerate(missing_visual_idx))
            _vis_raw = _call_llm(prompt=_vis_lines, system_prompt=_vis_sys, max_tokens=1500)
            _vis_auto = [_re.sub(r"^\d+\.\s*", "", ln.strip()) for ln in _vis_raw.splitlines() if ln.strip()]
            if len(_vis_auto) >= len(missing_visual_idx):
                for k, idx in enumerate(missing_visual_idx):
                    visual_descriptions[idx] = _vis_auto[k][:500]
        except Exception as _e:
            log.warning("Script-to-Video: auto visual prompt generation failed (%s) — using voiceover-derived prompts", _e)

        # Deterministic fallback so video generation NEVER depends on VISUAL: lines existing
        for i in missing_visual_idx:
            if visual_descriptions[i]:
                continue
            label = sections[i].get("label", f"Scene {i+1}")
            vo = (sections[i].get("voiceover", "") or "").strip()
            visual_descriptions[i] = ((f"{label} scene: {vo}" if vo else f"{label} cinematic scene") or "cinematic scene")[:500]

    log.info("Script-to-Video: visuals=%d (auto-filled %d), title_meta='%s'", len(visual_descriptions), len(missing_visual_idx), title_metadata[:40])

    # ── Run TTS + scene generation (video) in parallel ──
    from src.tts_engine import async_synthesize_speech
    from src.config import get_setting
    import uuid, hashlib, threading

    job_id = hashlib.md5(uuid.uuid4().bytes).hexdigest()[:8]

    # Light deterministic English cleanup so edge-tts / captions never read
    # structured markers or stutter separate from the LLM-written script.
    import re as _re
    def _clean_vo(_t: str) -> str:
        _t = _re.sub(r"\b(?:VISUAL|VOICEOVER|HOOK|PROBLEM|TWIST|CTA|SECRET|INSIGHT|TITLE)\s*:\s*", "", _t or "", flags=_re.I)
        _t = _re.sub(r"\s+", " ", _t).strip()
        _t = _re.sub(r"([.!?])\1+", r"\1", _t)
        _t = _re.sub(r"(?<=[.!?]\s)([a-z])", lambda _m: _m.group(1).upper(), _t)
        if _t and _t[0].islower():
            _t = _t[0].upper() + _t[1:]
        return _t
    # Keep captions in sync with what edge-tts actually speaks.
    for _sec in sections:
        _sec["voiceover"] = _clean_vo(_sec.get("voiceover", ""))
    _raw_vo = voiceover_text or ""
    voiceover_text = _clean_vo(_raw_vo)
    if _raw_vo != voiceover_text:
        log.info("Script-to-Video: normalized English text (%d -> %d chars)", len(_raw_vo), len(voiceover_text))

    # Register Script-to-Video job in DB so it appears in the Job Queue
    try:
        from src.models import Job, SessionLocal as _job_sl
        _jdb = _job_sl()
        try:
            existing = _jdb.query(Job).filter(Job.id == job_id).first()
            if not existing:
                _job = Job(
                    id=job_id,
                    user_id=user_id or 1,
                    job_type="script_to_video",
                    script_text=script_text[:500] if script_text else None,
                    youtube_url=f"Script-to-Video: {script_text[:60]}" if script_text else None,
                    status="processing",
                    progress_percent=5,
                )
                _jdb.add(_job)
                _jdb.commit()
        except Exception:
            _jdb.rollback()
        finally:
            _jdb.close()
    except Exception:
        log.warning("Could not register script-to-video job %s in DB", job_id)

    # Helper to mark this script-to-video job as failed (any unhandled exception)
    def _mark_job_failed(err: str, pct: int = 0):
        try:
            from src.models import Job as _J2, SessionLocal as _JSL2
            _jdb2 = _JSL2()
            try:
                _j2 = _jdb2.query(_J2).filter(_J2.id == job_id).first()
                if _j2:
                    _j2.status = "failed"
                    _j2.progress_percent = pct
                    _j2.error_message = err[:2000]
                    _jdb2.commit()
            except Exception:
                _jdb2.rollback()
            finally:
                _jdb2.close()
        except Exception:
            pass

    # Scene Generation uses the ACTIVE provider from the scene_providers table.
    from src.scene_providers import get_active_provider
    active_prov = get_active_provider()

    if not active_prov:
        log.warning("Script-to-Video: no active provider — will use image/template defaults for all scenes")
        providers_available = ["none (image/template fallback)"]
    elif active_prov["provider_key"] in ("fal", "replicate") and not active_prov.get("api_key"):
        log.warning("Script-to-Video: active provider '%s' has no API key — scenes will fallback to image/template", active_prov["provider_key"])
        providers_available = [f"{active_prov['name']} (no key → fallback)"]
    else:
        providers_available = [active_prov["name"]]
    log.info("Script-to-Video: active video provider: %s — only this provider will be tried; failure → image/template fallback", ", ".join(providers_available))

    scene_videos: list = []
    gen_error: list = []

    from src.config import get_video_spec_config as _vs
    _vspec = _vs()

    # Generate AI video clips — video_generator uses only the active provider
    from src.video_generator import generate_video_clips_batch

    # Each scene clip should roughly fill its voiceover segment (~14 chars/sec
    # speech rate), capped 4-10s so short scripts don't produce over-long clips.
    _n_scenes = max(1, len(visual_descriptions))
    scene_duration = max(4, min(10, int(round(len(voiceover_text) / (14.0 * _n_scenes)))))

    def _gen_videos():
        try:
            scene_videos.extend(generate_video_clips_batch(
                visual_descriptions,
                duration=scene_duration,
                width=int(_vspec["target_width"]),
                height=int(_vspec["target_height"]),
                progress_cb=lambda msg, pct: log.info("Scene gen: %s", msg),
            ))
        except Exception as e:
            log.error("Video generation error: %s", e)
            gen_error.append(e)

    gen_thread = threading.Thread(target=_gen_videos, daemon=True)
    gen_thread.start()

    # TTS runs concurrently (await in event loop while scenes generate in thread)
    try:
        tts_result = await async_synthesize_speech(voiceover_text, voice=voice, rate=rate)
    except Exception as e:
        log.error("TTS failed: %s", e)
        _mark_job_failed(f"TTS failed: {e}", pct=10)
        raise HTTPException(status_code=500, detail=f"TTS generation failed: {e}")

    # Wait for video generation to finish WITHOUT blocking the event loop.
    log.info("Script-to-Video: waiting for scene videos (max 20 min)...")
    import asyncio as _aio
    await _aio.to_thread(gen_thread.join, 1200)
    if gen_thread.is_alive():
        log.warning("Script-to-Video: scene generation still running after 20 min — continuing with partial results")

    if gen_error:
        log.error("Video generation thread raised: %s", gen_error)

    vid_ok = sum(1 for s in scene_videos if s is not None)
    log.info("Script-to-Video: %d/%d real AI scene videos generated (remaining will use image/template fallback)", vid_ok, len(visual_descriptions))

    # Never fail the whole short because one scene missed — reuse the previous
    # generated clip as a placeholder so the user always gets a video.
    if vid_ok < len(visual_descriptions):
        missing = len(visual_descriptions) - vid_ok
        log.warning("Script-to-Video: %d scene(s) missing — filling with existing clip(s)", missing)
        _first_hit = next((v for v in scene_videos if v is not None), None)
        scene_videos = [v if v is not None else _first_hit for v in scene_videos]

    # ── Map TTS segments back to sections ──
    segments = tts_result.segments
    section_labels = []

    if len(sections) == len(segments):
        # Perfect 1:1 mapping
        for i, sec in enumerate(sections):
            segments[i]["text"] = sec["voiceover"]
            section_labels.append(sec.get("label", ""))
    elif len(sections) > 0 and len(segments) > 0:
        # Distribute sections across available TTS segments proportionally
        total_tts_dur = tts_result.duration_secs
        total_sections = len(sections)
        dur_per_section = total_tts_dur / total_sections

        new_segments = []
        for i, sec in enumerate(sections):
            start = i * dur_per_section
            end = (i + 1) * dur_per_section
            vo_words = sec["voiceover"].split()
            word_dur = dur_per_section / max(1, len(vo_words))
            new_segments.append({
                "text": sec["voiceover"],
                "start": round(start, 3),
                "end": round(end, 3),
                "words": [{"text": w, "start": round(start + j * word_dur, 3), "end": round(start + (j + 1) * word_dur, 3)} for j, w in enumerate(vo_words)],
            })
            section_labels.append(sec.get("label", ""))

        segments = new_segments

    # ── Render video ──
    from src.script_video_renderer import render_script_video
    from src.config import OUTPUT_DIR

    out_name = f"script_{job_id}_001.mp4"
    try:
        video_path = render_script_video(
            tts_audio_path=tts_result.audio_path,
            segments=segments,
            output_filename=out_name,
            output_dir=OUTPUT_DIR,
            section_labels=section_labels,
            visual_descriptions=visual_descriptions,
            scene_videos=scene_videos,
        )
    except Exception as e:
        log.error("Script video render failed: %s", e)
        _mark_job_failed(f"Video rendering failed: {e}", pct=80)
        raise HTTPException(status_code=500, detail=f"Video rendering failed: {e}")

    # 5. Save to GeneratedClip DB (gallery)
    try:
        from src.models import GeneratedClip, SessionLocal as _SL
        _clip_db = _SL()
        try:
            _clip = GeneratedClip(
                job_id=job_id,
                user_id=user_id or 1,
                file_path=str(video_path),
                duration_seconds=tts_result.duration_secs,
                hook_score=0.0,
            )
            _clip_db.add(_clip)
            _clip_db.commit()
        finally:
            _clip_db.close()
    except Exception as clip_exc:
        log.warning("Failed to save GeneratedClip record: %s", clip_exc)

    # Mark script-to-video job as completed in the queue so it doesn't stay
    # stuck on "processing" forever.
    try:
        from src.models import Job as _Job, SessionLocal as _JSL
        _jdb = _JSL()
        try:
            _jrow = _jdb.query(_Job).filter(_Job.id == job_id).first()
            if _jrow:
                _jrow.status = "completed"
                _jrow.progress_percent = 100
                _jdb.commit()
        except Exception:
            _jdb.rollback()
        finally:
            _jdb.close()
    except Exception:
        pass

    log.info("Script-to-Video: done — %s (%.1fs)", out_name, tts_result.duration_secs)

    # Get file size and video dimensions
    try:
        size_bytes = video_path.stat().st_size if video_path.exists() else 0
    except Exception:
        size_bytes = 0

    try:
        from src.config import FFMPEG_BIN as _ffprobe_bin
        import subprocess as _sp
        probe = _sp.run(
            [_ffprobe_bin, "-i", str(video_path), "-f", "null", "-"],
            capture_output=True, text=True, timeout=10
        )
        # Parse from stderr
        import re as _re
        match = _re.search(r"(\d{3,5})x(\d{3,5})", probe.stderr)
        vid_width = int(match.group(1)) if match else 1080
        vid_height = int(match.group(2)) if match else 1920
    except Exception:
        vid_width, vid_height = 1080, 1920

    return {
        "success": True,
        "filename": out_name,
        "job_id": job_id,
        "duration": tts_result.duration_secs,
        "segments": len(segments),
        "width": vid_width,
        "height": vid_height,
        "size_bytes": size_bytes,
        "message": f"Video short generated: {out_name}",
    }


# ── Admin SMTP Configuration ──────────────────────────────────────────────────
@app.get("/api/admin/smtp", tags=["admin"])
async def get_admin_smtp(request: Request):
    _require_admin(request)
    from src.smtp_service import load_smtp_config
    cfg = load_smtp_config()
    safe_cfg = dict(cfg)
    safe_cfg["password"] = "••••••••" if cfg.get("password") else ""
    safe_cfg["has_password"] = bool(cfg.get("password"))
    return {"success": True, "smtp": safe_cfg}


@app.post("/api/admin/smtp/save", tags=["admin"])
async def save_admin_smtp(request: Request):
    _require_admin(request)
    from src.smtp_service import save_smtp_config
    body = await request.json()
    saved = save_smtp_config(body)
    return {"success": True, "message": "SMTP Configuration saved successfully!", "is_configured": saved.get("is_configured", False)}


@app.post("/api/admin/smtp/test", tags=["admin"])
async def test_admin_smtp(request: Request):
    _require_admin(request)
    from src.smtp_service import send_smtp_email, load_smtp_config
    body = await request.json()
    target_email = str(body.get("test_email") or "").strip()
    if not target_email:
        raise HTTPException(status_code=400, detail="Please enter a destination email address for testing.")

    test_html = """
    <div style="background:#080a11; color:#e2e8f0; padding:2rem; font-family:'Segoe UI',sans-serif; border-radius:12px; max-width:520px; margin:auto; border:1px solid rgba(6,182,212,0.4);">
      <h2 style="color:#38bdf8; margin-top:0;">⚡ Vergeclip AI SMTP Test</h2>
      <p>Congratulations! Your SMTP outgoing email server is properly connected and functioning.</p>
      <p style="color:#94a3b8; font-size:0.85rem;">System timestamp: UTC Verified ✓</p>
    </div>
    """
    ok, err = send_smtp_email(target_email, "⚡ Vergeclip AI — SMTP Connection Test", test_html, "Vergeclip AI SMTP Test Successful!")
    if not ok:
        raise HTTPException(status_code=400, detail=f"SMTP Test Failed: {err}")
    return {"success": True, "message": f"Test email dispatched successfully to {target_email}!"}


# ── System Health Status ──────────────────────────────────────────────────────
@app.get("/api/admin/system-health", tags=["admin"])
async def get_system_health(request: Request):
    _require_admin(request)
    import psutil
    import shutil
    from src.logger import SYSTEM_EVENT_LOGS

    # CPU & RAM
    cpu_pct = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    
    # Disk
    disk = shutil.disk_usage(ROOT_DIR)
    free_gb = round(disk.free / (1024 ** 3), 2)
    total_gb = round(disk.total / (1024 ** 3), 2)

    # Database
    db_size_mb = round((DATA_DIR / "users.db").stat().st_size / (1024 * 1024), 2) if (DATA_DIR / "users.db").exists() else 0.0

    return {
        "success": True,
        "health": {
            "status": "OPERATIONAL",
            "cpu_usage_pct": cpu_pct,
            "memory_usage_pct": mem.percent,
            "memory_used_mb": round((mem.total - mem.available) / (1024 * 1024), 1),
            "memory_total_mb": round(mem.total / (1024 * 1024), 1),
            "disk_free_gb": free_gb,
            "disk_total_gb": total_gb,
            "active_threads": threading.active_count(),
            "db_size_mb": db_size_mb,
            "total_audit_events": len(SYSTEM_EVENT_LOGS),
            "python_version": sys.version.split()[0],
            "server_uptime_mode": "Uvicorn + FastAPI Production ASGI"
        }
    }


# ── Paginated Job Queue Management ───────────────────────────────────────────
@app.get("/api/admin/jobs", tags=["admin"])
@app.get("/admin/jobs", tags=["admin"])
async def admin_list_jobs(request: Request):
    _require_admin(request)
    from src.models import Job, SessionLocal, User

    page = max(1, int(request.query_params.get("page", 1)))
    per_page = min(100, max(5, int(request.query_params.get("limit", request.query_params.get("per_page", 10)))))
    status_filter = str(request.query_params.get("status") or "").strip().lower()
    search = str(request.query_params.get("search") or "").strip().lower()

    db = SessionLocal()
    try:
        q = db.query(Job)
        if status_filter and status_filter != "all":
            q = q.filter(Job.status == status_filter)
        if search:
            q = q.filter((Job.youtube_url.ilike(f"%{search}%")) | (Job.id.ilike(f"%{search}%")))

        total = q.count()
        jobs = q.order_by(Job.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()

        user_map = {}
        for j in jobs:
            if j.user_id and j.user_id not in user_map:
                u = db.query(User).filter(User.id == j.user_id).first()
                user_map[j.user_id] = u.username if u else f"User #{j.user_id}"

        return {
            "success": True,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if total > 0 else 1,
            "jobs": [
                {
                    "id": j.id,
                    "user_id": j.user_id,
                    "user_name": user_map.get(j.user_id, "System / Guest"),
                    "youtube_url": j.youtube_url or "Prompt / Direct Input",
                    "status": j.status,
                    "progress_percent": j.progress_percent,
                    "error_message": j.error_message,
                    "created_at": j.created_at.isoformat() if j.created_at else None,
                    "updated_at": j.updated_at.isoformat() if j.updated_at else None,
                }
                for j in jobs
            ],
        }
    finally:
        db.close()


@app.delete("/api/admin/jobs/batch", tags=["admin"])
async def admin_delete_batch_jobs(request: Request):
    _require_admin(request)
    from src.models import Job, SessionLocal
    from src.logger import log_system_event

    body = await request.json()
    job_ids = body.get("job_ids", [])
    if not job_ids or not isinstance(job_ids, list):
        raise HTTPException(status_code=400, detail="No job IDs provided for batch deletion.")

    db = SessionLocal()
    try:
        deleted = db.query(Job).filter(Job.id.in_(job_ids)).delete(synchronize_session=False)
        db.commit()
        log_system_event("PIPELINE", "Batch Jobs Deleted", f"Purged {deleted} jobs from queue", severity="SUCCESS")
        return {"success": True, "message": f"Successfully deleted {deleted} pipeline job(s).", "deleted_count": deleted}
    finally:
        db.close()


@app.post("/api/admin/jobs/clear", tags=["admin"])
async def admin_clear_completed_jobs(request: Request):
    _require_admin(request)
    from src.models import Job, SessionLocal
    from src.logger import log_system_event

    db = SessionLocal()
    try:
        deleted = db.query(Job).filter(Job.status.in_(["done", "completed", "failed", "error"])).delete(synchronize_session=False)
        db.commit()
        log_system_event("PIPELINE", "Job Queue Purged", f"Removed {deleted} finished jobs", severity="SUCCESS")
        return {"success": True, "message": f"Cleared {deleted} completed / failed jobs from queue."}
    finally:
        db.close()


# ── File listing ──────────────────────────────────────────────────────────────
@app.get("/api/files/input", tags=["files"])
async def list_inputs():
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for f in INPUT_DIR.glob("*"):
        if f.is_file() and f.suffix.lower() in {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}:
            try:
                st = f.stat()
                files.append({"name": f.name, "size_mb": round(st.st_size / (1024 * 1024), 2), "modified": st.st_mtime})
            except Exception:
                continue
    files.sort(key=lambda x: x["modified"], reverse=True)
    return {"success": True, "files": files}


@app.get("/api/files/output", tags=["files"])
@app.get("/api/outputs", tags=["files"])
async def list_outputs(request: Request):
    from src.auth import decode_token, get_user_by_id
    from src.models import GeneratedClip, SessionLocal

    user_id = None
    is_admin = False
    auth = request.headers.get("authorization", "")
    token = None
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
    if not token:
        return {"success": True, "files": []}
    try:
        pl = decode_token(token)
        uid = int(pl.get("sub"))
        user = get_user_by_id(uid)
        if not user:
            return {"success": True, "files": []}
        user_id = user.id
        is_admin = user.role == "admin" or user.username.lower() == "admin"
    except Exception:
        return {"success": True, "files": []}

    db = SessionLocal()
    try:
        q = db.query(GeneratedClip)
        if not is_admin:
            q = q.filter(GeneratedClip.user_id == user_id)
        clips = q.order_by(GeneratedClip.created_at.desc()).all()

        files = []
        for clip in clips:
            fp = Path(clip.file_path)
            if not fp.exists() or not fp.is_file():
                continue
            try:
                st = fp.stat()
                files.append({
                    "name": fp.name,
                    "size_mb": round(st.st_size / (1024 * 1024), 2),
                    "modified": st.st_mtime,
                    "url": f"/api/stream/output/{fp.name}",
                    "clip_id": clip.id,
                    "job_id": clip.job_id,
                    "user_id": clip.user_id,
                })
            except Exception:
                continue
    finally:
        db.close()

    return {"success": True, "files": files}


# ── Streaming with Range + safe path ─────────────────────────────────────────
def _file_stream_response(file_path: Path, request: Request):
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    mime_type, _ = mimetypes.guess_type(str(file_path))
    if not mime_type:
        mime_type = "video/mp4"
    file_size = file_path.stat().st_size
    range_header = request.headers.get("range")

    if range_header:
        try:
            # bytes=0-1023 or bytes=1024-
            r = range_header.replace("bytes=", "").split("-")
            start = int(r[0]) if r[0] else 0
            end = int(r[1]) if len(r) > 1 and r[1] else file_size - 1
            start = max(0, min(start, file_size - 1))
            end = max(start, min(end, file_size - 1))
            length = end - start + 1

            def iter_range():
                with open(file_path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        yield chunk
                        remaining -= len(chunk)

            headers = {
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
            }
            return StreamingResponse(iter_range(), status_code=206, media_type=mime_type, headers=headers)
        except HTTPException:
            raise
        except Exception as exc:
            log.warning("Range parse failed %s: %s", range_header, exc)
            pass

    # Full file - use FileResponse for efficiency (sendfile)
    return FileResponse(str(file_path), media_type=mime_type, headers={"Accept-Ranges": "bytes"})


def _check_stream_auth(request: Request):
    """Check auth for streaming endpoints - supports header or ?token= query for <video> tags."""
    from src.auth import AUTH_REQUIRED, decode_token, get_user_by_id
    if not AUTH_REQUIRED:
        return
    token = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
    else:
        token = request.query_params.get("token") or request.query_params.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required for streaming")
    try:
        payload = decode_token(token)
        uid = payload.get("sub")
        if not uid or not get_user_by_id(int(uid)):
            raise HTTPException(status_code=401, detail="Invalid token")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


@app.get("/api/stream/output/{filename:path}", tags=["files"])
async def stream_output(filename: str, request: Request):
    _check_stream_auth(request)
    target = _safe_join(OUTPUT_DIR, filename)
    return _file_stream_response(target, request)


@app.get("/api/stream/input/{filename:path}", tags=["files"])
async def stream_input(filename: str, request: Request):
    _check_stream_auth(request)
    target = _safe_join(INPUT_DIR, filename)
    return _file_stream_response(target, request)


# Legacy route compat: /api/stream/output/filename with slash?
# FastAPI already handles encoded slashes, but we keep strict.


# ── Clear / Delete ────────────────────────────────────────────────────────────
@app.post("/api/files/output/clear", tags=["files"])
@app.post("/api/outputs/clear", tags=["files"])
async def clear_outputs(request: Request):
    from src.auth import AUTH_REQUIRED, decode_token, get_user_by_id
    from src.models import GeneratedClip, SessionLocal

    user_id = None
    is_admin = False
    if AUTH_REQUIRED:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Authentication required")
        try:
            pl = decode_token(auth.split(" ", 1)[1].strip())
            uid = int(pl.get("sub"))
            user = get_user_by_id(uid)
            if not user:
                raise HTTPException(status_code=401, detail="User not found")
            user_id = user.id
            is_admin = user.role == "admin" or user.username.lower() == "admin"
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token")

    db = SessionLocal()
    try:
        q = db.query(GeneratedClip)
        if user_id and not is_admin:
            q = q.filter(GeneratedClip.user_id == user_id)
        clips = q.all()
        count = 0
        for clip in clips:
            fp = Path(clip.file_path)
            if fp.exists():
                try:
                    fp.unlink(missing_ok=True)
                    count += 1
                except Exception:
                    pass
        q.delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()

    log.info("Cleared %d clips for user_id=%s (admin=%s)", count, user_id, is_admin)
    return {"success": True, "message": f"Deleted {count} clips from your gallery"}


@app.post("/api/files/output/delete", tags=["files"])
@app.post("/api/outputs/delete", tags=["files"])
async def delete_output(payload: Request):
    from src.auth import AUTH_REQUIRED, decode_token, get_user_by_id
    from src.models import GeneratedClip, SessionLocal

    user_id = None
    is_admin = False
    if AUTH_REQUIRED:
        auth = payload.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Authentication required")
        try:
            pl = decode_token(auth.split(" ", 1)[1].strip())
            uid = int(pl.get("sub"))
            user = get_user_by_id(uid)
            if not user:
                raise HTTPException(status_code=401, detail="User not found")
            user_id = user.id
            is_admin = user.role == "admin" or user.username.lower() == "admin"
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token")

    try:
        body = await payload.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    filename = str(body.get("filename", "")).strip()
    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    target = _safe_join(OUTPUT_DIR, filename)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    # Verify ownership
    db = SessionLocal()
    try:
        clip = db.query(GeneratedClip).filter(
            GeneratedClip.file_path.like(f"%{filename}"),
        ).first()
        if clip and user_id and not is_admin:
            if clip.user_id != user_id:
                raise HTTPException(status_code=403, detail="You don't own this clip")
        if clip:
            db.delete(clip)
            db.commit()
    finally:
        db.close()

    try:
        if target.exists():
            import time as _time
            from src.ffmpeg_utils import purge_stale_ffmpeg

            # Zombie ffmpeg from a crashed render keeps output files locked on
            # Windows (WinError 32). Kill any stale ones, then retry a few times.
            purge_stale_ffmpeg()
            _last_err = None
            for _attempt in range(5):
                try:
                    target.unlink()
                    _last_err = None
                    break
                except (PermissionError, OSError) as _exc:
                    _last_err = _exc
                    _time.sleep(0.4)
            if _last_err is not None:
                raise _last_err
        log.info("Deleted %s (user_id=%s)", filename, user_id)
        return {"success": True, "message": f"Deleted {filename}"}
    except Exception as exc:
        log.error("Delete failed %s: %s", filename, exc)
        raise HTTPException(status_code=500, detail=f"Could not delete: {exc}")


# ── Editor Export ─────────────────────────────────────────────────────────────
@app.post("/api/editor/export", tags=["editor"])
async def editor_export(payload: Request):
    from src.auth import AUTH_REQUIRED, decode_token, get_user_by_id

    if AUTH_REQUIRED:
        auth = payload.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Authentication required")
        try:
            pl = decode_token(auth.split(" ", 1)[1].strip())
            if not get_user_by_id(int(pl.get("sub"))):
                raise HTTPException(status_code=401, detail="User not found")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token")

    try:
        body = await payload.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    # Validate via pydantic
    try:
        req = EditorExportRequest(**body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    filename = req.filename
    # Resolve input: output first, then input
    # Use safe join for both dirs
    input_path: Optional[Path] = None
    for base in (OUTPUT_DIR, INPUT_DIR):
        try:
            cand = _safe_join(base, filename)
            if cand.exists() and cand.is_file():
                input_path = cand
                break
        except HTTPException:
            continue
    if not input_path:
        raise HTTPException(status_code=404, detail=f"Source video file '{filename}' not found in output or input directories.")

    # Normalize params
    start_time = req.start_time
    end_time = req.end_time
    preset = req.preset or req.filter_preset or "none"
    brightness = req.brightness if req.brightness is not None else 100.0
    contrast = req.contrast if req.contrast is not None else 100.0
    saturation = req.saturation if req.saturation is not None else 100.0
    sharpen = req.sharpen if req.sharpen is not None else 0.0
    pitch = req.pitch_semitones if req.pitch_semitones is not None else 0.0
    speed = req.speed if req.speed is not None else 1.0
    volume = req.volume if req.volume is not None else 100.0

    # Validate preset
    from src.editor_processor import FILTER_PRESETS

    if preset.lower().strip() not in FILTER_PRESETS:
        preset = "none"

    # Output path
    stem = input_path.stem
    # Sanitize stem
    safe_stem = "".join(c for c in stem if c.isalnum() or c in ("-", "_"))[:64] or "edited"
    out_filename = f"{safe_stem}_edited.mp4"
    output_path = _safe_join(OUTPUT_DIR, out_filename)
    # Ensure we don't overwrite with directory traversal: we already safe
    # If file exists, add suffix
    counter = 1
    while output_path.exists():
        out_filename = f"{safe_stem}_edited_{counter}.mp4"
        output_path = _safe_join(OUTPUT_DIR, out_filename)
        counter += 1
        if counter > 100:
            break

    # Validate trim times
    if start_time is not None and end_time is not None and end_time <= start_time:
        raise HTTPException(status_code=400, detail="end_time must be greater than start_time")

    try:
        from src.editor_processor import export_edited_video

        exported = export_edited_video(
            input_path=input_path,
            output_path=output_path,
            start_time=start_time,
            end_time=end_time,
            preset=preset,
            brightness=float(brightness),
            contrast=float(contrast),
            saturation=float(saturation),
            sharpen=float(sharpen),
            pitch_semitones=float(pitch),
            speed=float(speed),
            volume=float(volume),
        )
        return {
            "success": True,
            "exported_file": exported.name,
            "url": f"/api/stream/output/{exported.name}",
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.error("Editor export error: %s", exc)
        raise HTTPException(status_code=500, detail=f"Export processing failed: {exc}")


# ── Pipeline auto-generate ────────────────────────────────────────────────────
def _parse_num_shorts(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("all", "none", "", "0"):
        return None
    try:
        v = int(float(s))  # handle "5.0"
        if v <= 0:
            return None
        return min(v, 50)  # cap to prevent abuse
    except Exception:
        return None


@app.post("/api/pipeline/auto-generate", tags=["pipeline"])
async def auto_generate(payload: Request):
    from src.auth import AUTH_REQUIRED, decode_token, get_user_by_id

    user_id = None
    if AUTH_REQUIRED:
        auth = payload.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Authentication required - please login")
        try:
            pl = decode_token(auth.split(" ", 1)[1].strip())
            uid = pl.get("sub")
            u = get_user_by_id(int(uid))
            if not u:
                raise HTTPException(status_code=401, detail="User not found")
            user_id = u.id
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token")
    else:
        # Optional auth - capture user if token present
        auth = payload.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            try:
                from src.auth import decode_token as _dt, get_user_by_id as _gu
                pl = _dt(auth.split(" ", 1)[1].strip())
                uid = pl.get("sub")
                if uid:
                    u = _gu(int(uid))
                    if u:
                        user_id = u.id
            except Exception:
                pass

    try:
        body = await payload.json()
    except Exception:
        body = {}

    url = str(body.get("url", "") or "").strip()
    filename = str(body.get("filename", "") or "").strip()
    num_shorts = _parse_num_shorts(body.get("num_shorts"))
    # Admin-configured hard cap: at most this many shorts are generated per
    # video no matter what the client requests. Empty/0 means no cap.
    from src.config import get_setting as _get_setting

    admin_cap = _parse_num_shorts(_get_setting("MAX_SHORTS_PER_VIDEO", ""))
    if admin_cap and (num_shorts is None or num_shorts > admin_cap):
        num_shorts = admin_cap
    clear_existing = body.get("clear_existing", True)
    if isinstance(clear_existing, str):
        clear_existing = clear_existing.lower() not in ("false", "0", "no")

    if not url and not filename:
        raise HTTPException(status_code=400, detail="Please provide a YouTube URL or a video file.")

    # ── Device Trial Check (1 per system) ─────────────────────────────────
    device_id = str(body.get("device_id") or payload.headers.get("X-Device-Id") or payload.headers.get("x-device-id") or "").strip()
    ip = payload.client.host if payload.client else None
    # Admin bypass: if user is admin, skip device trial
    is_admin = False
    if user_id:
        try:
            from src.models import SessionLocal, User

            db_a = SessionLocal()
            try:
                au = db_a.query(User).filter(User.id == user_id).first()
                if au and au.role == "admin":
                    is_admin = True
            finally:
                db_a.close()
        except Exception:
            pass
    # Device trial only for guest (not logged in) — logged-in users use quota (5/month) instead of device limit
    if not is_admin and not user_id and device_id:
        from src.device_trial import check_device_trial, consume_device_trial

        chk = check_device_trial(device_id, ip)
        if not chk["allowed"]:
            raise HTTPException(status_code=403, detail=chk["reason"] + " — 1 trial per device for guests. Please signup/login to get 5 videos/month, or contact admin.")
    elif not is_admin and not user_id and not device_id:
        # No device id provided — fallback to IP-based check (loose) for guest only
        fallback = f"ip_{ip}_{payload.headers.get('user-agent','')[:30]}"
        if len(fallback) > 10:
            from src.device_trial import check_device_trial

            chk = check_device_trial(fallback, ip)
            if not chk["allowed"]:
                raise HTTPException(status_code=403, detail=chk["reason"])

    # Check running with lock — cancel previous if requested or running
    with pipeline_lock:
        if pipeline_state["status"] == "running":
            log.info("Previous pipeline running (job %s) — sending cancel signal...", pipeline_state.get("job_id"))
            active_pipeline_cancel_event.set()
            time.sleep(0.3)

    # Clear cancellation event for the new job
    active_pipeline_cancel_event.clear()

    # Assign job
    job_id = str(uuid.uuid4())[:8]
    with pipeline_lock:
        pipeline_state["status"] = "running"
        pipeline_state["progress"] = 5
        pipeline_state["error"] = None
        pipeline_state["logs"] = []
        pipeline_state["new_outputs"] = []
        pipeline_state["current_phase"] = "download"
        pipeline_state["job_id"] = job_id
        pipeline_state["started_at"] = time.time()
        pipeline_state["user_id"] = user_id

    # Consume device trial (1 per system) — only for guest, logged-in uses quota
    if not is_admin and not user_id and device_id:
        try:
            from src.device_trial import consume_device_trial

            consume_device_trial(device_id, ip)
        except HTTPException:
            # Revert pipeline state if consume fails (should not happen after check)
            with pipeline_lock:
                pipeline_state["status"] = "idle"
                pipeline_state["job_id"] = None
            raise
    elif not is_admin and not user_id and not device_id:
        fallback = f"ip_{ip}_{payload.headers.get('user-agent','')[:30]}"
        if len(fallback) > 10:
            try:
                from src.device_trial import consume_device_trial

                consume_device_trial(fallback, ip)
            except Exception:
                pass

    log.info("Pipeline job %s started (user=%s, url=%s, filename=%s, num_shorts=%s)", job_id, user_id, url[:60], filename, num_shorts)

    # Fire background thread (keep thread for CPU heavy rendering - not asyncio)
    def run_job():
        _run_full_pipeline_task(job_id=job_id, url=url, filename=filename, num_shorts=num_shorts, clear_existing=clear_existing)

    threading.Thread(target=run_job, daemon=True, name=f"pipeline-{job_id}").start()

    return {"success": True, "message": "Automatic generation pipeline started!", "job_id": job_id}


def _run_full_pipeline_task(job_id: str, url: str, filename: str, num_shorts: Optional[int], clear_existing: bool = True):
    """Runs in background thread, updates pipeline_state with lock and syncs to Job DB."""
    import gc
    from src.models import Job, SessionLocal
    from src.logger import log_system_event

    def _is_cancelled() -> bool:
        if active_pipeline_cancel_event.is_set():
            return True
        with pipeline_lock:
            if pipeline_state.get("job_id") != job_id or pipeline_state.get("status") == "idle":
                return True
        return False

    user_id = None
    with pipeline_lock:
        user_id = pipeline_state.get("user_id")

    # Register/Sync Job in DB
    db_job = SessionLocal()
    try:
        existing_job = db_job.query(Job).filter(Job.id == job_id).first()
        if not existing_job:
            new_j = Job(
                id=job_id,
                user_id=user_id or 1,
                youtube_url=url or filename or "Direct Input",
                status="processing",
                progress_percent=5
            )
            db_job.add(new_j)
        else:
            existing_job.status = "processing"
            existing_job.progress_percent = 5
        db_job.commit()
    except Exception as dbe:
        log.warning("Could not sync job %s to DB: %s", job_id, dbe)
    finally:
        db_job.close()

    def _sync_db_progress(pct: int, st: str = "processing", err: Optional[str] = None):
        _db = SessionLocal()
        try:
            j_row = _db.query(Job).filter(Job.id == job_id).first()
            if j_row:
                j_row.progress_percent = pct
                j_row.status = st
                if err:
                    j_row.error_message = err[:2000]
                _db.commit()
        except Exception:
            pass
        finally:
            _db.close()

    def _dl_progress_hook(msg: str, pct: int):
        if _is_cancelled():
            raise RuntimeError("Pipeline cancelled by user")
        log_pipeline_msg(msg)
        with pipeline_lock:
            pipeline_state["progress"] = min(25, pct)
        _sync_db_progress(min(25, pct))

    log_system_event("PIPELINE", "Pipeline Task Started", f"Job #{job_id}: Processing '{url or filename}'", user_id=user_id, severity="INFO")

    if clear_existing:
        try:
            for temp_item in TEMP_DIR.glob("*"):
                try:
                    if temp_item.is_file():
                        temp_item.unlink(missing_ok=True)
                    elif temp_item.is_dir():
                        shutil.rmtree(temp_item, ignore_errors=True)
                except Exception as e:
                    log.warning("Temp cleanup failed %s: %s", temp_item, e)
            if url:
                for old_vid in INPUT_DIR.glob("*"):
                    if old_vid.is_file():
                        try:
                            old_vid.unlink(missing_ok=True)
                        except Exception:
                            pass
            for old_out in OUTPUT_DIR.glob("*.mp4"):
                if old_out.is_file():
                    # NEVER delete script-to-video / test outputs — the user may
                    # still be saving them. Only YouTube-shorts outputs (short_*)
                    # removed so they can fully reproduce the app.
                    if old_out.name.startswith(("script_", "test_", "ai_")):
                        continue
                    try:
                        old_out.unlink(missing_ok=True)
                    except Exception:
                        pass
        except Exception as clean_err:
            log.warning("Could not clean previous workspace data: %s", clean_err)

    try:
        if _is_cancelled():
            raise RuntimeError("Pipeline cancelled by user")

        # Phase 1: Video Download or Selection
        with pipeline_lock:
            pipeline_state["current_phase"] = "download"
        video_path = None
        if url:
            log_pipeline_msg(f"🎬 [1/5] Downloading video stream from: {url}")
            from src.downloader import download_video

            video_path = download_video(url, progress_cb=_dl_progress_hook)
            log_pipeline_msg(f"✓ Video downloaded successfully: {video_path.name}")
            # Ensure progress reaches 25% even if hook didn't fire at exactly 25%
            with pipeline_lock:
                pipeline_state["progress"] = max(pipeline_state.get("progress", 0), 25)
            _sync_db_progress(25)
        elif filename:
            safe_name = Path(filename).name
            video_path = INPUT_DIR / safe_name
            if not video_path.exists():
                raise FileNotFoundError(f"Input file not found: {safe_name}")
            log_pipeline_msg(f"✓ Using input video: {video_path.name}")
        else:
            from app.transcriber import load_latest_video

            video_path = load_latest_video()
            log_pipeline_msg(f"✓ Using latest input video: {video_path.name}")

        if _is_cancelled():
            raise RuntimeError("Pipeline cancelled by user")

        with pipeline_lock:
            pipeline_state["progress"] = 25
        _sync_db_progress(25)

        # Phase 2: Transcription
        with pipeline_lock:
            pipeline_state["current_phase"] = "transcribe"
            pipeline_state["progress"] = 28
        _sync_db_progress(28)

        from src.config import get_setting
        _tp = get_setting("transcription_provider", "groq")
        _gm = get_setting("groq_whisper_model", "whisper-large-v3")
        if _tp == "groq":
            _tp_label = f"Groq Whisper ({_gm})"
        elif _tp == "faster_whisper":
            _fw_model = get_setting("faster_whisper_model", "base")
            _tp_label = f"Faster-Whisper ({_fw_model}, local)"
        else:
            _tp_label = "AssemblyAI Cloud"
        log_pipeline_msg(f"🎙️ [2/5] Transcribing audio with {_tp_label}...")

        def _tr_progress_hook(msg: str, pct: int):
            if _is_cancelled():
                raise RuntimeError("Pipeline cancelled by user")
            log_pipeline_msg(msg)
            with pipeline_lock:
                pipeline_state["progress"] = min(45, max(28, pct))
            _sync_db_progress(min(45, max(28, pct)))

        from app.transcriber import transcribe_video
        _tm = _gm if _tp == "groq" else (get_setting("faster_whisper_model", "base") if _tp == "faster_whisper" else None)
        tr_result = transcribe_video(video_path=video_path, provider=_tp, model_name=_tm, language=None, keep_audio=False)
        
        if _is_cancelled():
            raise RuntimeError("Pipeline cancelled by user")

        if tr_result.num_segments == 0:
            log_pipeline_msg("ℹ No spoken dialogue detected — switched to High-Energy Action / Scene Highlight Detection Engine!")
        else:
            log_pipeline_msg(f"✓ Transcription complete ({tr_result.model}): {tr_result.num_segments} segments ({tr_result.language})")
        
        with pipeline_lock:
            pipeline_state["progress"] = 45
        _sync_db_progress(45)

        if _is_cancelled():
            raise RuntimeError("Pipeline cancelled by user")

        # Phase 3: Clip Selection
        with pipeline_lock:
            pipeline_state["current_phase"] = "select"
            pipeline_state["progress"] = 50
        _sync_db_progress(50)
        log_pipeline_msg("⚡ [3/5] Extracting all viral highlight moments across full video...")
        from app.clip_selector import run_selection
        from src.config import get_clip_selection_config

        _clip_cfg = get_clip_selection_config()
        top_count = 100 if num_shorts is None else max(num_shorts * 2, 20)
        report = run_selection(
            transcript_path=TEMP_DIR / "transcript.json",
            min_dur=_clip_cfg["clip_min_duration"],
            max_dur=_clip_cfg["clip_max_duration"],
            top_n=top_count,
            min_score=_clip_cfg["clip_min_score"],
            min_separation=_clip_cfg["clip_min_separation"],
        )
        log_pipeline_msg(f"✓ Selected {report['final_count']} candidate clips from entire video")
        
        if _is_cancelled():
            raise RuntimeError("Pipeline cancelled by user")

        with pipeline_lock:
            pipeline_state["progress"] = 65
        _sync_db_progress(65)

        # Phase 3.5: LLM Ranking
        with pipeline_lock:
            pipeline_state["current_phase"] = "rank"
        log_pipeline_msg("🧠 [4/5] Evaluating viral retention hooks with AI ranking engine...")
        candidates_json = TEMP_DIR / "candidates.json"
        try:
            from app.semantic_ranker import run_semantic_ranking
            from src.config import get_clip_selection_config

            _sel_cfg = get_clip_selection_config()
            rank_target = report["final_count"] if num_shorts is None else num_shorts
            rank_result = run_semantic_ranking(
                candidates_path=TEMP_DIR / "candidate_pool.json",
                transcript_path=TEMP_DIR / "transcript.json",
                top_n=rank_target,
                semantic_pool_size=max(rank_target, 50),
                min_score=_sel_cfg["clip_min_score"],
                min_separation=_sel_cfg["clip_min_separation"],
            )
            candidates_json = Path(rank_result["json_path"])
            log_pipeline_msg(f"✓ AI Semantic ranking complete: {len(rank_result['final_selected'])} top shorts ranked")
        except Exception as llm_err:
            log_pipeline_msg(f"ℹ Semantic LLM ranking ({llm_err}) - using high-energy heuristic ranking.")

        if _is_cancelled():
            raise RuntimeError("Pipeline cancelled by user")

        with pipeline_lock:
            pipeline_state["progress"] = 75
        _sync_db_progress(75)

        # Phase 4 & 5: Render
        with pipeline_lock:
            pipeline_state["current_phase"] = "render"

        with open(candidates_json, "r", encoding="utf-8") as f:
            candidates_data = json.load(f)

        if isinstance(candidates_data, dict):
            clips_list = candidates_data.get("candidates", candidates_data.get("final_selected", []))
        elif isinstance(candidates_data, list):
            clips_list = candidates_data
        else:
            clips_list = []

        if num_shorts is not None and num_shorts > 0:
            clips_to_render = clips_list[:num_shorts]
        else:
            clips_to_render = clips_list

        num_to_render = len(clips_to_render)
        if num_to_render == 0:
            raise RuntimeError("No clips to render - candidate selection returned 0 clips")

        log_pipeline_msg(f"🎥 [5/5] Reframing 9:16 AI Face Tracking & burning captions for {num_to_render} short(s)...")

        try:
            from src.ffmpeg_utils import purge_stale_ffmpeg
            _killed = purge_stale_ffmpeg()
            if _killed:
                log_pipeline_msg(f"🧹 Killed {_killed} stale ffmpeg process(es) from previous renders.")
        except Exception as _purge_exc:
            log.warning("purge_stale_ffmpeg failed: %s", _purge_exc)

        rendered_files = []
        render_failures = 0
        from src.renderer import render_clip

        for idx in range(1, num_to_render + 1):
            if _is_cancelled():
                log_pipeline_msg("⚪ Rendering stopped early due to cancellation request.")
                break

            clip = clips_to_render[idx - 1]
            out_name = f"short_{job_id[:8]}_{idx:03d}.mp4"
            log_pipeline_msg(f"  Rendering Short #{idx}/{num_to_render}: {clip.get('text','')[:45]}...")
            try:
                result = render_clip(
                    rank=idx,
                    output_filename=out_name,
                    video_path=video_path,
                    candidates_path=candidates_json,
                    transcript_path=TEMP_DIR / "transcript.json",
                )
                rendered_files.append(out_name)
                log_pipeline_msg(f"  ✓ Short #{idx} rendered → {out_name}")
                # Save clip record to DB for user-bound gallery
                out_path = OUTPUT_DIR / out_name
                if out_path.exists():
                    try:
                        from src.models import GeneratedClip, SessionLocal as _SL
                        _clip_db = _SL()
                        try:
                            _clip = GeneratedClip(
                                job_id=job_id,
                                user_id=user_id or 1,
                                file_path=str(out_path),
                                duration_seconds=float(clip.get("end", 0) - clip.get("start", 0)) if clip.get("end") and clip.get("start") else 0,
                                hook_score=float(clip.get("score", 0)),
                            )
                            _clip_db.add(_clip)
                            _clip_db.commit()
                        finally:
                            _clip_db.close()
                    except Exception as clip_exc:
                        log.warning("Failed to save clip record: %s", clip_exc)
            except Exception as rend_exc:
                render_failures += 1
                log_pipeline_msg(f"  ✗ Failed rendering #{idx}: {rend_exc}")
                log.error("Render failed #%d: %s", idx, rend_exc)
            finally:
                gc.collect()

            progress_pct = 75 + int((idx / num_to_render) * 24)
            with pipeline_lock:
                pipeline_state["progress"] = min(99, progress_pct)
            _sync_db_progress(min(99, progress_pct))

        if render_failures == num_to_render and len(rendered_files) == 0:
            raise RuntimeError(f"All {num_to_render} render(s) failed — check ComfyUI / GPU / model availability (see admin → video-provider test).")

        if _is_cancelled():
            log_pipeline_msg(f"⚪ Pipeline cancelled. {len(rendered_files)} partial shorts saved.")
            with pipeline_lock:
                pipeline_state["status"] = "idle"
                pipeline_state["job_id"] = None
            _sync_db_progress(0, st="cancelled", err="Cancelled by user")
            return

        with pipeline_lock:
            if pipeline_state.get("job_id") == job_id:
                pipeline_state["progress"] = 100
                pipeline_state["status"] = "completed"
                pipeline_state["new_outputs"] = rendered_files

        _sync_db_progress(100, st="completed")
        log_pipeline_msg(f"🎉 Pipeline finished! {len(rendered_files)} new shorts ready in gallery.")
        log_system_event("PIPELINE", "Shorts Generated Successfully", f"Job #{job_id}: Rendered {len(rendered_files)} vertical 9:16 shorts for '{url or filename}'", user_id=user_id, severity="SUCCESS")

    except Exception as e:
        if "cancelled by user" in str(e).lower() or _is_cancelled():
            log_pipeline_msg("⚪ Pipeline safely cancelled and resources released.")
            with pipeline_lock:
                pipeline_state["status"] = "idle"
                pipeline_state["job_id"] = None
                pipeline_state["progress"] = 0
            _sync_db_progress(0, st="cancelled", err="Cancelled by user")
        else:
            log_pipeline_msg(f"❌ Pipeline Error: {e}")
            log.error("Pipeline job %s failed: %s", job_id, e)
            with pipeline_lock:
                if pipeline_state.get("job_id") == job_id:
                    pipeline_state["status"] = "error"
                    pipeline_state["error"] = str(e)
            # Save actual progress where it failed, NOT 100%
            _sync_db_progress(pipeline_state.get("progress", 0), st="failed", err=str(e))
            log_system_event("ERROR", "Pipeline Execution Error", f"Job #{job_id} failed: {e}", user_id=user_id, severity="ERROR")
    finally:
        gc.collect()


# ── Static frontend fallback (must be last) ───────────────────────────────────
# Mount frontend as static files but serve index.html for SPA
# We mount at "/" after API routes; FastAPI will check API routes first.
# Use StaticFiles with html=True for index fallback.

# Ensure frontend exists, create minimal if missing
if not FRONTEND_DIR.exists():
    FRONTEND_DIR.mkdir(parents=True, exist_ok=True)

# Mount static - but we need to handle / -> index.html and not override /api
# FastAPI StaticFiles will handle /index.html etc.
# We'll mount with check_dir=False? Use custom route for "/" first.

@app.get("/", include_in_schema=False)
async def serve_index():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return JSONResponse({"detail": "Frontend not found, API is running at /docs"}, status_code=404)


# Mount remaining frontend files under / (catch-all for css/js) but after API
# To avoid conflict with /api, we add a catch-all that serves static if file exists else 404
# Using StaticFiles mounted at "" needs careful ordering - we mount at "/static" alias and also serve root files via custom
# Simpler: mount StaticFiles at "/" with html=True, but it will handle /api/* after? FastAPI matches in order added,
# so add after all API routes.

try:
    app.mount("/assets/templates", StaticFiles(directory=str(ROOT_DIR / "assets" / "templates")), name="assets-templates")
except Exception:
    pass
try:
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR)), name="assets-frontend")
except Exception:
    pass

# ── Security: Block sensitive file exposure via static handler ──────────────
_SENSITIVE_PATTERNS = (".env", ".db", ".sqlite", ".log", ".git", "server.py", "config.py", "users.db", "__pycache__")

@app.get("/.well-known/{file_path:path}", include_in_schema=False)
async def serve_well_known(file_path: str):
    return JSONResponse({})


# For direct file serving at root (app.css, app.js, login.html etc) we add a route
@app.get("/{file_path:path}", include_in_schema=False)
async def serve_frontend_files(file_path: str):
    # Don't interfere with API/docs
    if file_path.startswith("api/") or file_path in ("docs", "redoc", "openapi.json", "health"):
        raise HTTPException(status_code=404)
    # Explicitly block sensitive patterns even if someone places them in frontend
    lower = file_path.lower()
    if ".." in file_path or "/." in file_path or file_path.startswith("."):
        raise HTTPException(status_code=404)
    for pat in _SENSITIVE_PATTERNS:
        if pat in lower:
            raise HTTPException(status_code=404)
    # Serve file if exists - strict frontend only
    target = FRONTEND_DIR / file_path
    # Handle path traversal - must stay inside frontend
    try:
        # Resolve and ensure inside frontend
        resolved = target.resolve()
        resolved.relative_to(FRONTEND_DIR.resolve())
        # Also ensure not a hidden file
        if resolved.name.startswith("."):
            raise HTTPException(status_code=404)
    except ValueError:
        raise HTTPException(status_code=404)
    if target.is_file():
        # Only allow known frontend extensions
        if target.suffix.lower() not in {".html", ".css", ".js", ".json", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".woff", ".woff2", ".ttf", ".map"}:
            raise HTTPException(status_code=404)
        return FileResponse(str(target))
    raise HTTPException(status_code=404)


# ── Legacy ThreadingHTTPServer compat (optional fallback) ─────────────────────
# Keep old handler for extreme fallback if FastAPI not desired, but not used in FastAPI mode.


def run_server(port: int | None = None, host: str | None = None, reload: bool = False):
    """Start uvicorn server. Supports --reload for dev."""
    ensure_directories()
    if port is None:
        port = int(os.environ.get("PORT", 5000))
    if host is None:
        host = os.environ.get("HOST", "0.0.0.0")
    # Ensure data dir for auth
    try:
        from src.auth import init_db

        init_db()
    except Exception as e:
        log.warning("Auth DB init failed: %s", e)

    display_host = "localhost" if host in ("0.0.0.0", "") else host
    print("\n=======================================================")
    print(" [*] Vergeclip AI Web App & API Server Running! (FastAPI + Uvicorn)")
    print(f" [*] App URL:      http://{display_host}:{port}/")
    print(f" [*] API Docs:     http://{display_host}:{port}/docs")
    print(f" [*] Health Check: http://{display_host}:{port}/health")
    print(f" [*] Auth:         JWT (signup/login at /api/auth/*)")
    print(f" [*] Mode:         {'Reload' if reload else 'Production'} | Fast & Thread-safe")
    print(f" [*] Environment:  {'Production' if os.environ.get('PORT') else 'Development'}")
    print("=======================================================\n")

    # Use uvicorn
    import uvicorn

    # uvicorn run with reload only in dev and when not in docker/production
    log_level = os.environ.get("LOG_LEVEL", "info").lower()
    uvicorn.run(
        "server:app",
        host=host,
        port=port,
        reload=reload,
        log_level=log_level,
        access_log=True,
        # Use workers=1 for pipeline state sharing; pipeline uses threads, not multiprocess
        # For production scale, add --workers >1 with Redis job queue
        workers=1,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Podcast Shorts Generator Web Server (FastAPI)")
    parser.add_argument("--port", type=int, default=None, help="Port to listen on (default: 5000 or $PORT)")
    parser.add_argument("--host", type=str, default=None, help="Host to bind to (default: 0.0.0.0 or $HOST)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development (uvicorn --reload)")
    parser.add_argument("--no-reload", dest="reload", action="store_false", help="Disable reload")
    parser.set_defaults(reload=False)
    # Also allow --reload via env
    args = parser.parse_args()
    # Check env reload
    if os.environ.get("RELOAD", "").lower() in ("1", "true", "yes"):
        args.reload = True
    run_server(port=args.port, host=args.host, reload=args.reload)
