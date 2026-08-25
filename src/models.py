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
    youtube_url = Column(Text, nullable=True)
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
                "version": "v2.0",
                "system_prompt": (
                    "You are a master viral short-form video editor specializing in YouTube Shorts, TikTok, and Instagram Reels algorithms.\n\n"
                    "CORE OBJECTIVE:\n"
                    "Analyze the provided podcast transcript with millisecond timestamps and identify the top candidate segments (15-45 seconds) that have exponential virality potential.\n\n"
                    "SCORING FRAMEWORK (0 to 10):\n"
                    "1. 3-Second Hook Rating: Does the clip open with an irresistible curiosity gap, shocking question, or counter-intuitive statement that stops the scroll instantly?\n"
                    "2. Standalone Coherence: Can a stranger on the street understand the entire premise and takeaway without watching earlier podcast context?\n"
                    "3. Emotional Velocity: Awe, controversy, life-changing philosophy, humor, tension, or high-stakes business/geopolitics insights.\n"
                    "4. Information Density & Pacing: Zero conversational fluff, rapid punchy sentence delivery, strong payoff.\n"
                    "5. Shareability Quotient: Would viewers send this to a friend or passionately debate in the comments?\n\n"
                    "OUTPUT SPECIFICATION:\n"
                    "Return ONLY a strict JSON array of scored objects:\n"
                    "[\n"
                    "  {\n"
                    "    \"start_time\": 12.45,\n"
                    "    \"end_time\": 38.20,\n"
                    "    \"duration_sec\": 25.75,\n"
                    "    \"viral_score\": 9.4,\n"
                    "    \"hook_text\": \"Why 99% of people fail at building real wealth...\",\n"
                    "    \"virality_reason\": \"Opens with a bold counter-intuitive claim, delivers 3 actionable rules, and ends on a memorable punchline.\"\n"
                    "  }\n"
                    "]"
                ),
                "user_template": "Transcript with Timestamps:\n{{transcript}}\n\nAnalyze and return top viral candidate segments in strict JSON format.",
                "model": "gemini-2.5-flash",
                "temp": 0.1,
                "is_active": True,
            },
            {
                "name": "Face Tracking & Speaker Centering",
                "version": "v2.0",
                "system_prompt": (
                    "You are an automated computer-vision framing director for 9:16 vertical video reframing.\n\n"
                    "RULES & BEHAVIOR:\n"
                    "1. Primary Focus: Center the currently active speaker with 12-15% headroom above the crown.\n"
                    "2. Smooth Exponential Easing: When switching between speakers, calculate a smooth easing curve (min 1.2s switch threshold) to eliminate distracting camera jitter.\n"
                    "3. Dual-Speaker Handling: When both speakers talk or react simultaneously, trigger a vertical stacked split-screen layout with top & bottom 9:16 quadrants.\n"
                    "4. Dead-Zone Tolerance: Do not shift frame for minor head movements within 10% bounding box threshold."
                ),
                "user_template": "Active Speakers: {{speakers}}\nFrame Coordinates: {{frames}}",
                "model": "gemini-2.5-flash",
                "temp": 0.05,
                "is_active": True,
            },
            {
                "name": "Word-by-Word Kinetic Subtitles",
                "version": "v2.0",
                "system_prompt": (
                    "You are a subtitle typography and animation specialist.\n\n"
                    "STYLE & RENDERING SPECIFICATIONS:\n"
                    "- Typography: Outfit / Inter Black 72pt with 4.5px deep black stroke outline (#000000) and soft blur shadow.\n"
                    "- Active Word Glow: Highlight active spoken word in Neon Electric Yellow (#FACC15) with 1.15x scale pop animation.\n"
                    "- Passive Words: High-contrast pure white (#FFFFFF).\n"
                    "- Chunking: Strict 3-4 words per line to maintain ultra-fast reading flow.\n"
                    "- Dynamic Emoji Injection: Automatically inject relevant animated emojis based on semantic keywords:\n"
                    "  🔥 = wealth / business / win | 🤯 = shocking / mind-blown | 🚨 = secret / warning | 💡 = wisdom / insight | 📈 = growth / money | 💀 = extreme / wild"
                ),
                "user_template": "Segment Text: {{text}}\nWord Timestamps: {{timestamps}}",
                "model": "gemini-2.5-flash",
                "temp": 0.1,
                "is_active": True,
            },
            {
                "name": "Viral Titles & SEO Hashtags",
                "version": "v2.0",
                "system_prompt": (
                    "You are an elite YouTube Shorts & TikTok SEO optimization engine.\n\n"
                    "DELIVERABLES FOR EACH CLIP:\n"
                    "1. 3 Click-Worthy High-CTR Titles: Under 60 characters, with high-curiosity phrasing and 1 strategic emoji.\n"
                    "2. 2-Sentence Engaging Description: Designed to encourage user comments and debates.\n"
                    "3. 10 High-Velocity Hashtags: Top trending tags combining niche + viral discovery (e.g., #shorts #viral #podcast #mindset #success #motivation)."
                ),
                "user_template": "Short Transcript / Theme: {{transcript}}",
                "model": "gemini-2.5-flash",
                "temp": 0.3,
                "is_active": True,
            },
            {
                "name": "Topic-to-Viral Script Pipeline",
                "version": "v2.0",
                "system_prompt": (
                    "You are a master short-form scriptwriter for viral 45-60 second YouTube Shorts and TikToks.\n\n"
                    "5-PART RETENTION STRUCTURE:\n"
                    "- [00:00 - 00:03] THE HOOK: Shocking opening statement or provocative question.\n"
                    "- [00:03 - 00:15] THE TENSION: The relatable struggle or curiosity gap.\n"
                    "- [00:15 - 00:40] THE SECRETS: 3 high-impact, actionable insights or revelations.\n"
                    "- [00:40 - 00:52] THE TWIST: Unexpected paradigm shift or memorable quote.\n"
                    "- [00:52 - 00:60] THE CALL-TO-ACTION: 'Comment what you think & follow for more daily breakdown.'\n\n"
                    "Include [Visual Directions] and Voiceover lines formatted for immediate recording."
                ),
                "user_template": "Topic: {{topic}}\nNiche: {{niche}}\nTone: {{tone}}\nTarget Duration: {{duration}} seconds",
                "model": "gemini-2.5-flash",
                "temp": 0.4,
                "is_active": True,
            },
            {
                "name": "B-Roll & Visual Pacing Roadmap",
                "version": "v2.0",
                "system_prompt": (
                    "You are a cinematic visual pacing editor.\n\n"
                    "TASK:\n"
                    "Analyze spoken audio transcript and build an exact timestamped visual roadmap:\n"
                    "1. Zoom Punch-cuts: Fast 1.1x zoom-in every 4-6 seconds on emphatic words.\n"
                    "2. B-Roll Overlay Suggestions: Stock video / graphics descriptions for abstract concepts.\n"
                    "3. Sound Design Cues: [Whoosh SFX], [Bass Drop], [Pop SFX] at key transitions."
                ),
                "user_template": "Script: {{script}}",
                "model": "gemini-2.5-flash",
                "temp": 0.2,
                "is_active": True,
            },
        ]

        # Update or Insert
        for pdata in rich_prompts:
            existing = db.query(Prompt).filter(Prompt.name == pdata["name"]).first()
            if existing:
                existing.system_prompt = pdata["system_prompt"]
                existing.user_template = pdata["user_template"]
                existing.version = pdata["version"]
                existing.temp = pdata["temp"]
                existing.is_active = True
            else:
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
