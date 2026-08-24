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

# ── Thread-safe pipeline state ────────────────────────────────────────────────
pipeline_lock = threading.Lock()
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
    return {"success": True, "message": "Password changed successfully"}


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
    with pipeline_lock:
        if pipeline_state["status"] != "running":
            return {"success": True, "message": "No pipeline running", "pipeline": dict(pipeline_state)}
        old_job = pipeline_state.get("job_id")
        pipeline_state["status"] = "idle"
        pipeline_state["current_phase"] = None
        pipeline_state["progress"] = 0
        pipeline_state["error"] = None
        log.info("Pipeline job %s cancelled by user", old_job)
        log_pipeline_msg(f"⚪ Pipeline cancelled (job {old_job})")
        # keep logs but reset job
        pipeline_state["job_id"] = None
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
ALLOWED_CONFIG_KEYS = {"VIDEOSAILOR_API_KEY", "ASSEMBLYAI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "RANKING_PROVIDER"}


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
        from src.config import save_api_config, get_all_api_config

        save_api_config(filtered)
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
    # Check role via DB (src/models)
    from src.models import SessionLocal, User

    db = SessionLocal()
    try:
        db_user = db.query(User).filter(User.id == uid).first()
        if not db_user or db_user.role != "admin":
            raise HTTPException(status_code=403, detail="Admin access required")
    finally:
        db.close()
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
        from src.config import settings, GOOGLE_API_KEY, OPENAI_API_KEY, ASSEMBLYAI_API_KEY, VIDEOSAILOR_API_KEY
        defaults = {
            "GOOGLE_API_KEY": GOOGLE_API_KEY,
            "OPENAI_API_KEY": OPENAI_API_KEY,
            "ASSEMBLYAI_API_KEY": ASSEMBLYAI_API_KEY,
            "VIDEOSAILOR_API_KEY": VIDEOSAILOR_API_KEY
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
    version = str(body.get("version", "")).strip()
    system_prompt = str(body.get("system_prompt", "")).strip()
    user_template = str(body.get("user_template", "")).strip()
    model = str(body.get("model", "")).strip() or "gemini-3.6-flash"
    temp = float(body.get("temp", 0.1))
    if not name or not version or not system_prompt:
        raise HTTPException(status_code=400, detail="name, version, system_prompt required")
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
        # Quick Gemini/OpenAI test if is ranker and has API key
        test_result = {"rendered_preview": rendered[:800], "prompt_length": len(p.system_prompt), "model": p.model, "verified": True, "message": "Prompt template rendered successfully (no LLM call in test mode). Use Activate to make live."}
        # Optionally do live LLM call if ?live=true
        if request.query_params.get("live") == "true":
            try:
                from app.semantic_ranker import _call_llm

                resp = _call_llm(prompt=f"{p.system_prompt}\n\nTranscript: {sample[:800]}", system_prompt=None, provider="gemini")
                test_result["live_llm_response"] = resp[:500]
                test_result["message"] = "Live LLM call succeeded"
            except Exception as e:
                test_result["live_llm_error"] = str(e)[:500]
                test_result["verified"] = False
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
        return {"success": True, "message": f"Prompt {p.name}:{p.version} activated"}
    finally:
        db.close()


@app.get("/api/admin/audit", tags=["admin"])
@app.get("/admin/audit", tags=["admin"])
async def admin_audit(request: Request):
    _require_admin(request)
    from src.logger import SYSTEM_EVENT_LOGS
    from src.models import AuditLog, SessionLocal

    # 1. Real-time in-memory event stream (Zero delay)
    live_events = list(SYSTEM_EVENT_LOGS)

    # 2. Database audit records
    db = SessionLocal()
    db_logs = []
    try:
        logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(30).all()
        db_logs = [
            {
                "id": l.id,
                "admin_id": l.admin_id,
                "key": l.key,
                "old_value": (l.old_value[:50] + "...") if l.old_value and len(l.old_value) > 50 else l.old_value,
                "new_value": (l.new_value[:50] + "...") if l.new_value and len(l.new_value) > 50 else l.new_value,
                "tested": l.tested,
                "created_at": l.created_at.isoformat() if l.created_at else None
            }
            for l in logs
        ]
    finally:
        db.close()

    return {
        "success": True,
        "live_events": live_events,
        "logs": db_logs
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
    """Save custom OpenAI-compatible AI config into system environment."""
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

    log_system_event("CONFIG", "Primary AI Config Updated", f"Provider set to {provider} ({model_name} @ {base_url})", severity="SUCCESS")
    return {"success": True, "message": f"Custom AI Provider '{provider}' saved and activated for all video generation pipelines!"}


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


@app.post("/api/pipeline/generate-from-topic", tags=["pipeline"])
async def generate_from_topic(request: Request):
    """Generate viral short script and video pipeline from user prompt/topic (No video needed mode)."""
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
        "You are an expert AI scriptwriter for viral 45-60 second YouTube Shorts and TikToks. "
        "Write an explosive, retention-optimized short script with timestamped Hook, Problem, 3 Secrets, Twist, and CTA."
    )
    _db = SessionLocal()
    try:
        p_row = _db.query(Prompt).filter(Prompt.name == "Topic-to-Viral Script Pipeline", Prompt.is_active == True).first()
        if p_row and p_row.system_prompt:
            sys_prompt = p_row.system_prompt
    finally:
        _db.close()

    user_req = f"""Generate a viral {duration}-second short script on this topic:
TOPIC: {topic}
NICHE: {niche}
TONE: {tone}
TARGET DURATION: {duration} seconds

Format with clear [00:00 - 00:03] Hook, Problem, 3 Insights, Twist, and Call-to-Action, followed by Viral Title and 10 Hashtags."""

    try:
        script_result = _call_llm(prompt=user_req, system_prompt=sys_prompt)
        log_system_event("PIPELINE", "Topic Script Generated", f"Generated script for topic '{topic}' ({niche})", severity="SUCCESS")
    except Exception as e:
        log.warning("LLM script generation failed: %s", e)
        log_system_event("PIPELINE", "Topic Script Fallback", f"LLM error: {e}, using template", severity="WARN")

    if not script_result:
        # High quality fallback template if LLM key not configured
        script_result = f"""🔥 VIRAL TITLE: The Secret Truth About {topic} 🤯

HOOK (00:00 - 00:03):
"Almost nobody knows this, but {topic} is completely misunderstood..."

PROBLEM (00:03 - 00:12):
"99% of people struggle with this because they follow outdated advice that keeps them stuck."

CORE VALUE (00:12 - 00:38):
"Here are 3 game-changing rules:
1. Stop overcomplicating the basics.
2. Focus on high-leverage execution daily.
3. Master your emotional control before taking action."

TWIST (00:38 - 00:48):
"Once you apply this one subtle shift, everything accelerates 10x faster."

CALL TO ACTION (00:48 - 00:{duration:02d}):
"Drop a 🔥 in the comments if you agree, and follow for more daily wisdom!"

📱 HASHTAGS: #shorts #viral #{niche.replace(' ', '').lower()} #mindset #growth #trending"""

    return {
        "success": True,
        "topic": topic,
        "duration": duration,
        "niche": niche,
        "tone": tone,
        "generated_script": script_result,
        "message": "Viral Short Script generated successfully!"
    }


@app.get("/api/admin/jobs", tags=["admin"])
@app.get("/admin/jobs", tags=["admin"])
async def admin_list_jobs(request: Request):
    _require_admin(request)
    from src.models import Job, SessionLocal

    db = SessionLocal()
    try:
        jobs = db.query(Job).order_by(Job.created_at.desc()).limit(50).all()
        return {
            "success": True,
            "jobs": [{"id": j.id, "user_id": j.user_id, "youtube_url": (j.youtube_url[:60] + "...") if j.youtube_url and len(j.youtube_url) > 60 else j.youtube_url, "status": j.status, "progress_percent": j.progress_percent, "error_message": j.error_message, "created_at": j.created_at.isoformat() if j.created_at else None} for j in jobs],
        }
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
async def list_outputs():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for f in OUTPUT_DIR.glob("*.mp4"):
        if f.is_file():
            try:
                st = f.stat()
                files.append({"name": f.name, "size_mb": round(st.st_size / (1024 * 1024), 2), "modified": st.st_mtime, "url": f"/api/stream/output/{f.name}"})
            except Exception:
                continue
    files.sort(key=lambda x: x["modified"] if "modified" in x else x["name"], reverse=True)
    # Frontend expects sorted by name? Keep modified reverse for newest first, but stable
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
    # Auth if required
    from src.auth import AUTH_REQUIRED, decode_token, get_user_by_id

    if AUTH_REQUIRED:
        auth = request.headers.get("authorization", "")
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

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    for f in OUTPUT_DIR.glob("*.mp4"):
        try:
            f.unlink(missing_ok=True)
            count += 1
        except Exception:
            continue
    log.info("Cleared %d output files (requested by %s)", count, request.client.host if request.client else "unknown")
    return {"success": True, "message": f"Deleted {count} output files"}


@app.post("/api/files/output/delete", tags=["files"])
@app.post("/api/outputs/delete", tags=["files"])
async def delete_output(payload: Request):
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
    filename = str(body.get("filename", "")).strip()
    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    target = _safe_join(OUTPUT_DIR, filename)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        target.unlink()
        log.info("Deleted %s", filename)
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

    # Check running with lock
    with pipeline_lock:
        if pipeline_state["status"] == "running":
            return {"success": True, "message": "Pipeline already running", "pipeline": dict(pipeline_state)}

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
    """Runs in background thread, updates pipeline_state with lock."""
    # Clean previous if requested - but now per-job, don't nuke output if pipeline fails midway?
    # For backward compat we clear, but make it optional via clear_existing
    # If clear_existing False, we keep old outputs and just append new.
    if clear_existing:
        try:
            # 1. Clear temp
            for temp_item in TEMP_DIR.glob("*"):
                try:
                    if temp_item.is_file():
                        temp_item.unlink(missing_ok=True)
                    elif temp_item.is_dir():
                        shutil.rmtree(temp_item, ignore_errors=True)
                except Exception as e:
                    log.warning("Temp cleanup failed %s: %s", temp_item, e)
            # 2. If new URL, delete old input videos (optional but legacy behavior)
            if url:
                for old_vid in INPUT_DIR.glob("*"):
                    if old_vid.is_file():
                        try:
                            old_vid.unlink(missing_ok=True)
                        except Exception as e:
                            log.warning("Could not delete old input %s: %s", old_vid.name, e)
            # 3. Clean old outputs
            for old_out in OUTPUT_DIR.glob("*.mp4"):
                if old_out.is_file():
                    try:
                        old_out.unlink(missing_ok=True)
                    except Exception as e:
                        log.warning("Could not delete old output %s: %s", old_out.name, e)
        except Exception as clean_err:
            log.warning("Could not clean previous workspace data: %s", clean_err)
    else:
        # Minimal temp clear only temp (keep outputs)
        try:
            for temp_item in TEMP_DIR.glob("*"):
                try:
                    if temp_item.is_file():
                        temp_item.unlink(missing_ok=True)
                    elif temp_item.is_dir():
                        shutil.rmtree(temp_item, ignore_errors=True)
                except Exception:
                    pass
        except Exception as e:
            log.warning("Temp clear failed: %s", e)

    try:
        # Phase 1: Video Download or Selection
        with pipeline_lock:
            pipeline_state["current_phase"] = "download"
        video_path = None
        if url:
            log_pipeline_msg(f"🎬 [1/5] Downloading fresh video from: {url}")
            from src.downloader import download_video

            video_path = download_video(url)
            log_pipeline_msg(f"✓ Video downloaded: {video_path.name}")
        elif filename:
            # Safe filename handling for input
            safe_name = Path(filename).name
            video_path = INPUT_DIR / safe_name
            if not video_path.exists():
                raise FileNotFoundError(f"Input file not found: {safe_name}")
            log_pipeline_msg(f"✓ Using input video: {video_path.name}")
        else:
            from app.transcriber import load_latest_video

            video_path = load_latest_video()
            log_pipeline_msg(f"✓ Using latest input video: {video_path.name}")

        with pipeline_lock:
            pipeline_state["progress"] = 25

        # Phase 2: Transcription
        with pipeline_lock:
            pipeline_state["current_phase"] = "transcribe"
        log_pipeline_msg("🎙️ [2/5] Transcribing audio with AssemblyAI Cloud API...")
        from app.transcriber import transcribe_video

        tr_result = transcribe_video(video_path=video_path, language=None, keep_audio=False)
        if tr_result.num_segments == 0:
            log_pipeline_msg("ℹ No spoken dialogue detected — switched to High-Energy Action / Scene Highlight Detection Engine!")
        else:
            log_pipeline_msg(f"✓ Transcription complete: {tr_result.num_segments} segments ({tr_result.language})")
        with pipeline_lock:
            pipeline_state["progress"] = 45

        # Phase 3: Clip Selection
        with pipeline_lock:
            pipeline_state["current_phase"] = "select"
        if tr_result.num_segments == 0:
            log_pipeline_msg("⚡ [3/5] Detecting high-energy battle/action climaxes across full video...")
        else:
            log_pipeline_msg("⚡ [3/5] Extracting all key viral highlight moments across full video...")
        from app.clip_selector import run_selection

        top_count = 100 if num_shorts is None else max(num_shorts * 2, 20)
        report = run_selection(
            transcript_path=TEMP_DIR / "transcript.json",
            min_dur=15.0,
            max_dur=30.0,
            top_n=top_count,
            min_score=20.0,
            min_separation=20.0,
        )
        log_pipeline_msg(f"✓ Selected {report['final_count']} highlight clips from entire video")
        with pipeline_lock:
            pipeline_state["progress"] = 65

        # Phase 3.5: LLM Ranking
        with pipeline_lock:
            pipeline_state["current_phase"] = "rank"
        log_pipeline_msg("🧠 [4/5] Evaluating candidates with semantic AI ranking...")
        candidates_json = TEMP_DIR / "candidates.json"
        try:
            from app.semantic_ranker import run_semantic_ranking

            rank_target = report["final_count"] if num_shorts is None else num_shorts
            rank_result = run_semantic_ranking(
                candidates_path=TEMP_DIR / "candidate_pool.json",
                transcript_path=TEMP_DIR / "transcript.json",
                top_n=rank_target,
                semantic_pool_size=max(rank_target, 50),
                min_score=20.0,
                min_separation=20.0,
            )
            candidates_json = Path(rank_result["json_path"])
            log_pipeline_msg(f"✓ AI Semantic ranking complete: {len(rank_result['final_selected'])} top shorts ranked")
        except Exception as llm_err:
            log_pipeline_msg(f"ℹ Semantic LLM ranking ({llm_err}) - using high-energy heuristic ranking.")

        with pipeline_lock:
            pipeline_state["progress"] = 75

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

        log_pipeline_msg(f"🎥 [5/5] Reframing 9:16 AI Face Tracking & burning captions for all {num_to_render} shorts across full video...")

        rendered_files = []
        from src.renderer import render_clip

        for idx in range(1, num_to_render + 1):
            clip = clips_to_render[idx - 1]
            out_name = f"short_{idx:03d}.mp4"
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
            except Exception as rend_exc:
                log_pipeline_msg(f"  ✗ Failed rendering #{idx}: {rend_exc}")
                log.error("Render failed #%d: %s", idx, rend_exc)
            finally:
                import gc

                gc.collect()
            progress_pct = 75 + int((idx / num_to_render) * 24)
            with pipeline_lock:
                pipeline_state["progress"] = min(99, progress_pct)

        with pipeline_lock:
            # Only update if still same job (allows cancel to not be overwritten)
            if pipeline_state.get("job_id") == job_id:
                pipeline_state["progress"] = 100
                pipeline_state["status"] = "completed"
                pipeline_state["new_outputs"] = rendered_files
        log_pipeline_msg(f"🎉 Pipeline finished! {len(rendered_files)} new shorts ready in gallery.")

    except Exception as e:
        log_pipeline_msg(f"❌ Pipeline Error: {e}")
        log.error("Pipeline job %s failed: %s", job_id, e)
        with pipeline_lock:
            if pipeline_state.get("job_id") == job_id:
                pipeline_state["status"] = "error"
                pipeline_state["error"] = str(e)


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
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR)), name="assets-frontend")
except Exception:
    pass

# ── Security: Block sensitive file exposure via static handler ──────────────
_SENSITIVE_PATTERNS = (".env", ".db", ".sqlite", ".log", ".git", "server.py", "config.py", "users.db", "__pycache__")

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
