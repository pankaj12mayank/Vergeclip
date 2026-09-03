"""
src/models.py
-------------
SQLAlchemy models for Podcast Shorts Generator (Phase A2).

Supports both SQLite (dev, sqlite:///./data/users.db) and Postgres (prod).
All tables are created via Base.metadata.create_all() on import.
Existing raw sqlite `users` table is migrated automatically (adds role/tier if missing).

Models:
- User (id, email unique, hashed_password, created_at, tier, role)
- UsageQuota (user_id, month_year, videos_processed, videos_limit) UNIQUE user+month
- Job (id, user_id, youtube_url, filename, status, progress_percent, created_at, updated_at, error_message, prompt_version)
- GeneratedClip (id, job_id, user_id, file_path, duration_seconds, hook_score, thumbnail_path, created_at)
- Prompt (id, name, version, system_prompt, user_template, model, temp, is_active, created_by, created_at)
- Setting (id, key unique, value, is_secret, updated_by, updated_at)
- AuditLog (id, admin_id, key, old_value, new_value, tested, created_at)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from src.config import PROJECT_ROOT

# ── Engine from DATABASE_URL ──────────────────────────────────────────────────
try:
    from src.config import settings as _settings

    _db_url = getattr(_settings, "DATABASE_URL", None) if _settings else None
except Exception:
    _db_url = None

if not _db_url:
    _db_url = os.environ.get("DATABASE_URL", "sqlite:///./data/users.db")

# Normalize sqlite path to absolute
if _db_url.startswith("sqlite"):
    # sqlite:///./data/users.db -> sqlite:///M:/.../data/users.db
    if _db_url.startswith("sqlite:///./"):
        rel = _db_url.replace("sqlite:///./", "")
        abs_path = (PROJECT_ROOT / rel).resolve()
        _db_url = f"sqlite:///{abs_path.as_posix()}"
    elif _db_url == "sqlite:///./data/users.db":
        abs_path = (PROJECT_ROOT / "data" / "users.db").resolve()
        _db_url = f"sqlite:///{abs_path.as_posix()}"

# For SQLite, need check_same_thread=False
connect_args = {"check_same_thread": False} if _db_url.startswith("sqlite") else {}

engine = create_engine(_db_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ── Models ────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"
    # Keep existing table name `users` for backward compat with src/auth.py raw sqlite
    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(254), unique=True, nullable=False, index=True)
    username = Column(String(32), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    tier = Column(String(20), default="free", nullable=False)  # free | pro etc
    role = Column(String(20), default="user", nullable=False)  # user | admin
    is_active = Column(Boolean, default=True, nullable=False)

    quotas = relationship("UsageQuota", back_populates="user", cascade="all, delete-orphan")
    jobs = relationship("Job", back_populates="user", cascade="all, delete-orphan")


class UsageQuota(Base):
    __tablename__ = "usage_quotas"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    month_year = Column(String(7), nullable=False)  # "2026-08"
    videos_processed = Column(Integer, default=0, nullable=False)
    videos_limit = Column(Integer, default=5, nullable=False)

    user = relationship("User", back_populates="quotas")
    __table_args__ = (
        UniqueConstraint("user_id", "month_year", name="uq_user_month"),
        Index("idx_quota_user_month", "user_id", "month_year"),
    )


class Job(Base):
    __tablename__ = "jobs"
    id = Column(String(36), primary_key=True)  # uuid
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    job_type = Column(String(30), default="youtube", nullable=False)  # youtube | script_to_video
    youtube_url = Column(Text, nullable=True)
    script_text = Column(Text, nullable=True)
    filename = Column(String(255), nullable=True)
    status = Column(String(20), default="queued", nullable=False)  # queued/processing/done/failed
    progress_percent = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)
    error_message = Column(Text, nullable=True)
    prompt_version = Column(String(50), nullable=True)

    user = relationship("User", back_populates="jobs")
    clips = relationship("GeneratedClip", back_populates="job", cascade="all, delete-orphan")
    __table_args__ = (
        Index("idx_job_user_status", "user_id", "status"),
        Index("idx_job_status_created", "status", "created_at"),
    )


class GeneratedClip(Base):
    __tablename__ = "generated_clips"
    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    file_path = Column(String(500), nullable=False)
    duration_seconds = Column(Float, nullable=True)
    hook_score = Column(Float, nullable=True)
    thumbnail_path = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    job = relationship("Job", back_populates="clips")
    __table_args__ = (Index("idx_clip_job", "job_id"), Index("idx_clip_user", "user_id"))


class Prompt(Base):
    __tablename__ = "prompts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)  # ranker, clip_selector, caption, audio
    version = Column(String(50), nullable=False)
    system_prompt = Column(Text, nullable=False)
    user_template = Column(Text, nullable=True)
    model = Column(String(100), nullable=True)  # gemini-3.6-flash etc
    temp = Column(Float, default=0.1)
    category = Column(String(80), nullable=True)  # youtube_shorts | script_generation | script_based_shorts
    is_active = Column(Boolean, default=True, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    __table_args__ = (UniqueConstraint("name", "version", name="uq_prompt_name_version"),)


class Setting(Base):
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(Text, nullable=True)
    is_secret = Column(Boolean, default=False, nullable=False)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(20), unique=True, nullable=False, index=True)
    category = Column(String(50), nullable=False, index=True)
    action = Column(String(200), nullable=False)
    detail = Column(Text, nullable=True)
    user_id = Column(String(50), nullable=True)
    severity = Column(String(20), default="INFO", nullable=False)
    ip = Column(String(50), default="127.0.0.1")
    request_data = Column(Text, nullable=True)
    response_data = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)


class DeviceTrial(Base):
    __tablename__ = "device_trials"
    id = Column(Integer, primary_key=True, autoincrement=True)
    device_id = Column(String(128), unique=True, nullable=False, index=True)  # fingerprint hash
    ip_address = Column(String(45), nullable=True)
    trials_used = Column(Integer, default=0, nullable=False)
    max_trials = Column(Integer, default=1, nullable=False)  # 1 per system as per plan
    first_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    last_seen = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)
    is_blocked = Column(Boolean, default=False, nullable=False)
    __table_args__ = (Index("idx_device_id", "device_id"),)


class SceneProvider(Base):
    """Scene Generation (Script-to-Video) provider configuration.

    One active provider at a time. Provider list, active selection, API keys,
    model names and endpoint/timeout are all stored here (not hardcoded) so the
    admin UI can switch providers without a redeploy.
    """
    __tablename__ = "scene_providers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_key = Column(String(50), unique=True, nullable=False)  # local | fal | replicate
    name = Column(String(100), nullable=False)  # display name
    api_key = Column(String(500), nullable=True)  # plaintext (matches existing CustomProvider pattern)
    model_name = Column(String(200), nullable=True)  # e.g. wan-2.1-t2v / kuaishou/kling-video / replicate model id
    endpoint = Column(String(500), nullable=True)  # local ComfyUI URL, or fal/replicate base URL
    timeout_seconds = Column(Integer, default=120, nullable=False)
    is_active = Column(Boolean, default=False, nullable=False)  # only one active at a time
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)


class CustomProvider(Base):
    __tablename__ = "custom_providers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)  # display name: "Groq Free", "DeepSeek Production"
    provider_type = Column(String(50), nullable=False, default="custom_openai")  # groq | deepseek | openrouter | custom_openai | ollama
    base_url = Column(String(500), nullable=False)
    api_key = Column(String(500), nullable=True)
    model = Column(String(200), nullable=False)
    is_active = Column(Boolean, default=False, nullable=False)  # only one active at a time
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)


# ── Create tables + migrate existing users table ──────────────────────────────
def init_db():
    """Create all tables and migrate existing `users` if needed."""
    # Create all if not exists
    Base.metadata.create_all(bind=engine)

    # Migrate audit_logs table if schema changed (old schema had key/old_value/new_value, new has event_id/category/action)
    if str(engine.url).startswith("sqlite"):
        import sqlite3 as _sqlite3
        _db_path = str(engine.url).replace("sqlite:///", "")
        if _db_path.startswith("/") and ":" in _db_path[1:4]:
            _db_path = _db_path[1:]
        try:
            _conn = _sqlite3.connect(_db_path)
            _cur = _conn.execute("PRAGMA table_info(audit_logs)")
            _audit_cols = {row[1] for row in _cur.fetchall()}
            if "event_id" not in _audit_cols and _audit_cols:
                _conn.execute("DROP TABLE IF EXISTS audit_logs")
                _conn.commit()
            _conn.close()
        except Exception:
            pass

    # Migrate existing raw users table: add role/tier if missing (for SQLite)
    if str(engine.url).startswith("sqlite"):
        import sqlite3

        # Get raw DB path from url
        db_path = str(engine.url).replace("sqlite:///", "")
        # Handle Windows path with extra slash
        if db_path.startswith("/") and ":" in db_path[1:4]:
            db_path = db_path[1:]
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.execute("PRAGMA table_info(users)")
            cols = {row[1] for row in cur.fetchall()}
            if "role" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
                # Update existing admin to admin role if username admin
                conn.execute("UPDATE users SET role='admin' WHERE username='admin' AND (role IS NULL OR role='user')")
                conn.commit()
            if "tier" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN tier TEXT DEFAULT 'free'")
                conn.commit()
            conn.close()
        except Exception:
            pass

    # Migrate prompts table: add category column if missing (for SQLite)
    if str(engine.url).startswith("sqlite"):
        import sqlite3
        db_path = str(engine.url).replace("sqlite:///", "")
        if db_path.startswith("/") and ":" in db_path[1:4]:
            db_path = db_path[1:]
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.execute("PRAGMA table_info(prompts)")
            pcols = {row[1] for row in cur.fetchall()}
            if "category" not in pcols:
                conn.execute("ALTER TABLE prompts ADD COLUMN category VARCHAR(80)")
                conn.commit()
            # Migrate jobs table: add job_type and script_text columns
            cur = conn.execute("PRAGMA table_info(jobs)")
            jcols = {row[1] for row in cur.fetchall()}
            if "job_type" not in jcols:
                conn.execute("ALTER TABLE jobs ADD COLUMN job_type VARCHAR(30) DEFAULT 'youtube'")
                conn.execute("UPDATE jobs SET job_type = 'youtube' WHERE job_type IS NULL")
                conn.commit()
            if "script_text" not in jcols:
                conn.execute("ALTER TABLE jobs ADD COLUMN script_text TEXT")
                conn.commit()
            conn.close()
        except Exception:
            pass

    # Seed or upgrade default rich prompt pipelines
    from sqlalchemy.orm import Session
    db = SessionLocal()
    try:
        # Clean up legacy v1 test records
        db.query(Prompt).filter(Prompt.name.in_(["ranker", "caption"])).delete(synchronize_session=False)
        db.commit()

        rich_prompts = [
            {
                "name": "Viral Hook & Retention Ranker",
                "version": "v2.2",
                "category": "youtube_shorts",
                "system_prompt": (
                    "You are an elite viral short-form video editor who has built channels to 100M+ views. "
                    "Your ONLY job: find the segment that will go viral.\n\n"
                    "ANALYSIS PROCESS:\n"
                    "1. Read the ENTIRE transcript — understand full context\n"
                    "2. Identify TOP 5 candidate segments (15-45 seconds each)\n"
                    "3. Score each on 5 dimensions (0-10)\n"
                    "4. Pick #1 segment that will STOP THE SCROLL\n\n"
                    "SCORING (weighted): Hook Strength (30%) + Standalone Coherence (20%) + Emotional Impact (20%) + Pacing (15%) + Shareability (15%)\n\n"
                    "HOOK STRENGTH — First 3 seconds MUST:\n"
                    "- Open with SHOCKING claim or counter-intuitive statement\n"
                    "- Create irresistible curiosity gap\n"
                    "- Make viewer think 'Wait, WHAT?'\n\n"
                    "STANDALONE COHERENCE — Can someone understand WITHOUT watching the rest?\n"
                    "- Must have clear setup → tension → payoff arc\n"
                    "- No references to earlier context\n"
                    "- Self-contained story\n\n"
                    "OUTPUT: Return ONLY this JSON:\n"
                    "[{\"start_time\":12.45,\"end_time\":38.20,\"duration_sec\":25.75,\"viral_score\":9.4,\"hook_text\":\"Why 99% of people fail...\",\"virality_reason\":\"Opens with bold claim, delivers 3 rules, ends on punchline.\"}]"
                ),
                "user_template": "Full Transcript with Timestamps:\n{{transcript}}\n\nFind the TOP viral segment. Return ONLY the JSON array.",
                "model": "gemini-2.5-flash",
                "temp": 0.1,
                "is_active": True,
            },
            {
                "name": "Face Tracking & Speaker Centering",
                "version": "v2.2",
                "category": "youtube_shorts",
                "system_prompt": (
                    "You are a cinematic framing director. You convert 16:9 horizontal video into 9:16 vertical shorts "
                    "that look like they were SHOT vertically — not cropped.\n\n"
                    "THE GOLDEN RULE:\n"
                    "The output must look like a professional filmmaker chose to shoot in 9:16. "
                    "Never show empty black bars. Never cut off the speaker's head. Never lose the action.\n\n"
                    "FRAMING RULES:\n"
                    "1. FACE CENTERING: Active speaker's eyes at 35% from top (top third rule). "
                    "15% headroom above crown. Never clip the chin.\n"
                    "2. LOOK-AHEAD: When speaker will move/gesture, anticipate by 0.5s and pre-shift frame.\n"
                    "3. DUAL SPEAKERS: When both visible, compose vertical split with speaker emphasis (60/40 split).\n"
                    "4. JITTER KILLER: Dead-zone of 8% — never shift for micro-movements.\n"
                    "5. TRANSITIONS: Smooth exponential easing (0.8s min) between speaker switches. NO jumps.\n"
                    "6. ENVIRONMENT: If interesting background exists, shift frame to include it while keeping speaker centered.\n\n"
                    "OUTPUT FORMAT — One entry per second of video:\n"
                    "[{\"t\":0.0,\"x\":0.35,\"y\":0.20,\"w\":0.45,\"h\":1.0},...]\n"
                    "Coordinates normalized 0..1. x,y = top-left of 9:16 crop window. w=0.45 means crop takes 45% of source width."
                ),
                "user_template": "Speaker Timeline: {{speakers}}\nFace Coordinates: {{frames}}\n\nGenerate smooth 9:16 framing that looks NATURAL, not cropped.",
                "model": "gemini-2.5-flash",
                "temp": 0.05,
                "is_active": True,
            },
            {
                "name": "Word-by-Word Kinetic Subtitles",
                "version": "v2.2",
                "category": "youtube_shorts",
                "system_prompt": (
                    "You are the subtitle artist for MrBeast, Ali Abdaal, and top YouTube Shorts creators. "
                    "Your captions make people WATCH instead of scroll.\n\n"
                    "THE RULES:\n"
                    "1. CHUNK SIZE: Exactly 3-4 words per chunk. Never 5. Never 2.\n"
                    "2. LINE LIMIT: Maximum 2 lines on screen at once. If more words, split into new chunk.\n"
                    "3. SAFE ZONE: All text in bottom 40% of screen (y=60% to y=90%). Never overlap faces.\n"
                    "4. ACTIVE WORD: The word being SPOKEN right now gets Electric Yellow (#FACC15) + 1.2x scale.\n"
                    "5. PASSIVE WORDS: Pure white (#FFFFFF) with 4px black stroke.\n"
                    "6. FONT: Bold, 72px minimum at 1080x1920. Must be readable on phone.\n"
                    "7. EMOJI: Add ONE emoji per chunk ONLY for emotionally charged words:\n"
                    "   money/win/success | shock/surprise | secret/knowledge | danger/extreme\n"
                    "8. TIMING: Each chunk appears EXACTLY when the first word is spoken. Disappears when last word ends.\n\n"
                    "NEVER:\n"
                    "- Show more than 8 words on screen at once\n"
                    "- Place text above y=50% (overlaps faces)\n"
                    "- Add emojis to every chunk (max 1 per 3 chunks)\n"
                    "- Change word order from transcript\n\n"
                    "OUTPUT: Word-by-word map with start/end timestamps and chunk assignments."
                ),
                "user_template": "Transcript Text: {{text}}\nWord Timestamps: {{timestamps}}\n\nGenerate kinetic subtitles. 3-4 words per chunk. Bottom-safe zone only.",
                "model": "gemini-2.5-flash",
                "temp": 0.05,
                "is_active": True,
            },
            {
                "name": "Viral Titles & SEO Hashtags",
                "version": "v2.2",
                "category": "youtube_shorts",
                "system_prompt": (
                    "You are the YouTube Shorts SEO expert behind channels with 10M+ subscribers. "
                    "Your titles get CLICKS. Your hashtags get DISCOVERED.\n\n"
                    "TITLE FORMULA (use for each of 3 titles):\n"
                    "1. Start with a POWER NUMBER or bold claim\n"
                    "2. Create curiosity gap (make them NEED to know)\n"
                    "3. Under 50 characters (mobile-first)\n"
                    "4. Add ONE strategic emoji at start or end\n\n"
                    "TITLE EXAMPLES:\n"
                    "- '3 Money Rules Nobody Tells You'\n"
                    "- 'Why Smart People Stay Broke'\n"
                    "- 'The Secret Rich Won't Share'\n\n"
                    "DESCRIPTION (2 sentences):\n"
                    "- First sentence: provokes debate or asks a question\n"
                    "- Second sentence: teases the payoff\n\n"
                    "HASHTAGS (10 total):\n"
                    "- 3 broad: #shorts #viral #trending\n"
                    "- 3 niche: match the content topic\n"
                    "- 3 engagement: #mindset #success #motivation\n"
                    "- 1 branded: your channel name\n\n"
                    "OUTPUT: JSON with primary_title, alt_titles[2], description, hashtags[10]"
                ),
                "user_template": "Short Content:\n{{transcript}}\n\nGenerate 3 CTR-optimized titles, description, and hashtags.",
                "model": "gemini-2.5-flash",
                "temp": 0.3,
                "is_active": True,
            },
            {
                "name": "Topic-to-Viral Script Pipeline",
                "version": "v2.2",
                "category": "script_generation",
                "system_prompt": (
                    "You are a world-class short-form scriptwriter. You write scripts that get MILLIONS of views. "
                    "Every word is intentional. Every sentence hooks the viewer deeper.\n\n"
                    "THE SCRIPT FORMAT (MANDATORY — copy this exact structure):\n"
                    "TITLE: [Compelling title, under 50 chars]\n"
                    "TIMESTAMP: 00:00 - HH:MM\n\n"
                    "HOOK\n"
                    "TIMESTAMP: 00:00 - HH:MM\n"
                    "VISUAL: [Visual direction — what AI video clip shows here]\n"
                    "VOICEOVER: [Exact spoken line]\n\n"
                    "PROBLEM\n"
                    "TIMESTAMP: 00:XX - HH:MM\n"
                    "VISUAL: [Visual direction]\n"
                    "VOICEOVER: [Exact spoken line]\n\n"
                    "SECRET ONE\n"
                    "TIMESTAMP: 00:XX - HH:MM\n"
                    "VISUAL: [Visual direction]\n"
                    "VOICEOVER: [Exact spoken line]\n\n"
                    "SECRET TWO\n"
                    "TIMESTAMP: 00:XX - HH:MM\n"
                    "VISUAL: [Visual direction]\n"
                    "VOICEOVER: [Exact spoken line]\n\n"
                    "SECRET THREE\n"
                    "TIMESTAMP: 00:XX - HH:MM\n"
                    "VISUAL: [Visual direction]\n"
                    "VOICEOVER: [Exact spoken line]\n\n"
                    "TWIST\n"
                    "TIMESTAMP: 00:XX - HH:MM\n"
                    "VISUAL: [Visual direction]\n"
                    "VOICEOVER: [Exact spoken line]\n\n"
                    "CTA\n"
                    "TIMESTAMP: 00:XX - 00:XX\n"
                    "VISUAL: [Visual direction]\n"
                    "VOICEOVER: [Exact spoken line]\n\n"
                    "TIMING RULES (scale sections to match the target duration):\n"
                    "- 30 seconds: HOOK 3s, PROBLEM 5s, SECRETS 5s each, TWIST 4s, CTA 3s\n"
                    "- 45 seconds: HOOK 3s, PROBLEM 8s, SECRETS 7s each, TWIST 6s, CTA 4s\n"
                    "- 60 seconds: HOOK 4s, PROBLEM 10s, SECRETS 9s each, TWIST 8s, CTA 5s\n"
                    "- Adjust TIMESTAMP ranges to match the selected duration exactly\n"
                    "- The total script must match the TARGET DURATION (±2 seconds)\n\n"
                    "VOICEOVER RULES:\n"
                    "- Write for the EAR, not the eye — conversational, punchy, like talking to a smart friend\n"
                    "- Short sentences (under 10 words each)\n"
                    "- No emojis, no symbols, no markdown — pure spoken English\n"
                    "- Each secret must be actionable and specific (not generic advice)\n"
                    "- The TWIST must reframe everything the viewer just learned\n"
                    "- The CTA must feel natural, not salesy\n\n"
                    "VISUAL RULES:\n"
                    "- Write visual directions for AI video generation (cinematic, specific, vivid)\n"
                    "- Describe camera angle, lighting, mood, and subject\n"
                    "- Example: 'Wide shot of businessman standing alone in empty office at night, blue lighting'\n"
                    "- Never write generic visuals like 'relevant B-roll' — be specific\n"
                    "- NEVER write visuals about text, typography, zooming on text, animation, or on-screen UI.\n"
                    "  Every VISUAL must be a REAL filmed scene a text-to-video model can render as live footage\n"
                    "  (real people, environments, objects, camera motion).\n"
                    "- NEVER paste the topic/title into a VOICEOVER line — the spoken narration must read naturally,\n"
                    "  as if a host is talking about the topic, not reciting the prompt.\n\n"
                    "OUTPUT: ONLY the script in the exact format above. No explanations, no commentary."
                ),
                "user_template": "Topic: {{topic}}\nNiche: {{niche}}\nTone: {{tone}}\nTarget Duration: {{duration}} seconds\n\nWrite a {{duration}}-second viral script. Adjust all timestamps to match {{duration}} seconds exactly.",
                "model": "gemini-2.5-flash",
                "temp": 0.4,
                "is_active": True,
            },
            {
                "name": "B-Roll & Visual Pacing Roadmap",
                "version": "v2.2",
                "category": "script_based_shorts",
                "system_prompt": (
                    "You are a cinematic visual director who transforms scripts into STORIES. You don't just plan shots — you build worlds. "
                    "Your output should feel like a Netflix short film, not a slideshow.\n\n"
                    "YOUR JOB:\n"
                    "Take the script and create a VISUAL STORY that a viewer watches with their EYES, not just listens to. "
                    "Every second must have a PURPOSE. Every frame must TELL THE STORY.\n\n"
                    "THE 3-LAYER VISUAL SYSTEM:\n\n"
                    "LAYER 1 — THE HERO SHOT (70% of screen time):\n"
                    "This is the MAIN visual that carries the story forward. Think: a person walking through a city at night, "
                    "hands typing on a keyboard in a dark office, a sunrise over mountains. This is what the viewer SEES while listening.\n"
                    "Format: \"[SCENE] + [SUBJECT] + [ACTION] + [CAMERA] + [LIGHTING] + [MOOD]\"\n"
                    "Example: \"Modern office at night. Young businessman stands at floor-to-ceiling window, city lights reflecting on his face. "
                    "Slow push-in from waist-up. Blue ambient lighting. Mood: contemplative, ambitious.\"\n\n"
                    "LAYER 2 — THE EMOTION PUNCTUATION (20% of screen time):\n"
                    "Quick 1-2 second cuts that AMPLIFY the emotion of what's being said. Not random B-roll — these are VISUAL EXCLAMATION MARKS.\n"
                    "Examples:\n"
                    "- On 'money' → close-up of hands counting bills, rack focus\n"
                    "- On 'failure' → slow-motion of something falling/shattering\n"
                    "- On 'success' → silhouette of person raising arms at sunset\n"
                    "- On 'secret' → extreme close-up of eye, light flickering\n"
                    "Format: \"[QUICK CUT] + [CLOSE-UP/DETAIL] + [MOTION] + [EMOTION]\"\n\n"
                    "LAYER 3 — THE CINEMATIC GLUE (10% of screen time):\n"
                    "Transitions and pacing that make the video FEEL like a movie, not a PowerPoint.\n"
                    "- Use slow-motion (0.5x) for emotional peaks\n"
                    "- Use whip-pan for energy transitions\n"
                    "- Use rack focus to guide attention\n"
                    "- Use match-cuts to connect ideas visually\n\n"
                    "CHARACTER CONSISTENCY RULES:\n"
                    "- If the script has a 'protagonist', describe them ONCE in detail at the start\n"
                    "- Use the SAME character description throughout (same clothes, same face type, same age)\n"
                    "- Example: \"28-year-old man, short black hair, navy blue suit, white shirt, no tie\"\n"
                    "- NEVER switch characters mid-video unless the script explicitly changes speaker\n\n"
                    "CINEMATOGRAPHY LANGUAGE (use these terms):\n"
                    "- Camera: tracking shot, dolly in/out, crane shot, handheld, steadicam, orbit, whip-pan\n"
                    "- Framing: extreme close-up, close-up, medium shot, wide shot, establishing shot, over-the-shoulder\n"
                    "- Lighting: golden hour, blue hour, neon, chiaroscuro, rim light, silhouette, practical lighting\n"
                    "- Movement: slow-motion, time-lapse, speed ramp, freeze frame, push-in, pull-out\n\n"
                    "STORY STRUCTURE (follow the script's emotional arc):\n"
                    "- OPENING (first 3-5s): Establish the world. Wide shot of location. Set the mood.\n"
                    "- RISING (next 10-15s): Build tension. Characters in conflict. Quick cuts. Energy rising.\n"
                    "- PEAK (middle): The revelation. Slow-motion. Close-ups. Maximum emotion.\n"
                    "- FALLING (next 10-15s): Resolution. Wider shots. Calmer pacing. Light returns.\n"
                    "- ENDING (last 3-5s): Leave an image that haunts. Wide shot. Silence. Fade to black.\n\n"
                    "OUTPUT FORMAT (strict JSON array):\n"
                    "[\n"
                    "  {\n"
                    "    \"t_start\": 0.0,\n"
                    "    \"t_end\": 3.5,\n"
                    "    \"layer\": \"hero\",\n"
                    "    \"visual\": \"[Full cinematic description with subject, action, camera, lighting, mood]\",\n"
                    "    \"transition\": \"fade_in\"\n"
                    "  },\n"
                    "  {\n"
                    "    \"t_start\": 3.5,\n"
                    "    \"t_end\": 5.0,\n"
                    "    \"layer\": \"emotion\",\n"
                    "    \"visual\": \"[Quick cut close-up that amplifies the emotion]\",\n"
                    "    \"transition\": \"cut\"\n"
                    "  }\n"
                    "]\n\n"
                    "RULES:\n"
                    "- Every t_start must equal the previous t_end (no gaps)\n"
                    "- Hero shots should be 3-5 seconds minimum (not 1-second clips)\n"
                    "- Emotion shots are 1-2 seconds max\n"
                    "- ALWAYS end on a powerful wide shot that leaves the viewer thinking\n"
                    "- Visual descriptions must be SPECIFIC enough for AI video generation"
                ),
                "user_template": "Full Script with Timestamps:\n{{script}}\n\nCreate a cinematic visual story plan. Every frame must serve the narrative.",
                "model": "gemini-2.5-flash",
                "temp": 0.3,
                "is_active": True,
            },
        ]

        # Only INSERT new prompts if they don't already exist.
        # NEVER overwrite existing prompts — user edits must survive server restarts.
        for pdata in rich_prompts:
            existing = db.query(Prompt).filter(Prompt.name == pdata["name"]).first()
            if not existing:
                p = Prompt(**pdata)
                db.add(p)
        db.commit()

        # Seed default pipeline settings (only if not already set)
        import json as _json
        default_settings = {
            # Video Specs
            "target_width": "1080",
            "target_height": "1920",
            "target_fps": "30",
            "max_short_duration": "60",
            "min_short_duration": "15",
            # Clip Selection
            "clip_min_duration": "15.0",
            "clip_max_duration": "20.0",
            "clip_top_n": "30",
            "clip_min_score": "30.0",
            "clip_min_separation": "90.0",
            "clip_distribution_strategy": "spaced_top",
            "clip_step_size": "1.5",
            "clip_overlap_threshold": "0.4",
            # Scoring Weights (stored as JSON dict)
            "scoring_weights": _json.dumps({
                "hook_question": 22.0, "hook_bold_claim": 18.0, "hook_surprising": 18.0,
                "hook_formulaic_power": 16.0, "hook_imperative": 15.0, "hook_story_intro": 14.0,
                "hook_number_stat": 12.0, "body_question": 8.0, "body_contrast": 8.0,
                "body_educational": 8.0, "body_statistics": 7.0, "body_storytelling": 6.0,
                "body_emotional": 6.0, "body_surprise": 6.0, "standalone_subject_intro": 12.0,
                "complete_thought": 12.0, "clean_sentence_start": 8.0, "high_confidence": 5.0,
                "good_pacing": 4.0, "penalty_dangling_open": -25.0, "penalty_weak_filler_open": -20.0,
                "penalty_unresolved_pronoun": -18.0, "penalty_incomplete_end": -15.0,
                "penalty_context_dependent": -15.0, "penalty_filler_dense": -10.0,
                "penalty_low_confidence": -8.0, "penalty_sparse_words": -8.0,
            }),
            # Semantic Ranking
            "semantic_default_pool_size": "100",
            "semantic_min_score": "40.0",
            "semantic_default_top_n": "30",
            "semantic_default_separation": "90.0",
            # Caption Settings
            "caption_font_size": "82",
            "caption_max_words": "5",
            "caption_min_words": "2",
            "caption_max_lines": "2",
            "caption_max_width": "900",
            "caption_y": "1450",
            "caption_text_color": "255,255,255",
            "caption_highlight_color": "255,230,0",
            "caption_outline_color": "0,0,0",
            "caption_outline_width": "6",
            "caption_start_padding": "0.0",
            "caption_end_padding": "0.05",
            "caption_max_duration": "2.5",
            "caption_min_duration": "0.4",
            # Enhancement
            "auto_color_filter_enabled": "true",
            "auto_video_filter": "eq=contrast=1.06:saturation=1.12:brightness=0.01,unsharp=5:5:0.35:3:3:0.0",
            "auto_pitch_shift_enabled": "true",
            "auto_pitch_semitones": "0.5",
        }
        for skey, sval in default_settings.items():
            existing_setting = db.query(Setting).filter(Setting.key == skey).first()
            if not existing_setting:
                db.add(Setting(key=skey, value=sval, is_secret=False))
        db.commit()

        # ── Scene Generation provider migration ────────────────────────────────
        # Remove legacy provider settings (Pollinations.ai / Agnes AI / the old
        # single value in video_gen_provider). Provider state now lives in the
        # scene_providers table instead of flat settings keys.
        from src.models import SceneProvider
        legacy_keys = [
            "pollinations_api_key", "pollinations_api_key_is_set",
            "agnes_api_key", "video_gen_provider",
        ]
        db.query(Setting).filter(Setting.key.in_(legacy_keys)).delete(synchronize_session=False)
        db.commit()

        # Seed the three scene-generation providers (seed/insert-only so admin
        # edits to keys/models survive restarts).
        scene_seed = [
            {"provider_key": "local", "name": "Local — Wan2.1 / LTX-Video (your GPU, offline, free)",
             "api_key": None, "model_name": "wan-2.1-t2v-14b",
             "endpoint": "http://127.0.0.1:8188", "timeout_seconds": 700, "is_active": False},
            {"provider_key": "fal", "name": "fal.ai (cloud, pay-as-you-go)",
             "api_key": None, "model_name": "kuaishou/kling-video/v1/standard/text-to-video",
             "endpoint": "https://queue.fal.run", "timeout_seconds": 180, "is_active": False},
            {"provider_key": "replicate", "name": "Replicate (cloud, pay-as-you-go)",
             "api_key": None, "model_name": "wan-video/wan-2.1-t2v-14b",
             "endpoint": "https://api.replicate.com/v1", "timeout_seconds": 180, "is_active": False},
        ]
        for sp in scene_seed:
            existing_prov = db.query(SceneProvider).filter(SceneProvider.provider_key == sp["provider_key"]).first()
            if not existing_prov:
                db.add(SceneProvider(**sp))
        db.commit()

        # Ensure default admin has role admin
        from sqlalchemy import text as sql_text
        db.execute(sql_text("UPDATE users SET role='admin' WHERE username='admin' AND role != 'admin'"))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


# Init on import
try:
    init_db()
except Exception as e:
    # Don't crash on import, log warning
    try:
        from src.logger import get_logger

        get_logger("models").warning("DB init failed: %s", e)
    except Exception:
        pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
