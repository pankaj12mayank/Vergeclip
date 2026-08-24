"""
config.py
---------
Central configuration for the Podcast Shorts Generator.

All path constants and default settings live here so every module
can import from one place instead of hard-coding values.
"""

from pathlib import Path
import os

# Load .env file automatically so API keys never need to be hardcoded.
# Create/edit .env in the project root to change any key.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except ImportError:
    pass  # python-dotenv not installed; fall back to environment variables only

# ── Project Root ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── Directory Layout ──────────────────────────────────────────────────────────
INPUT_DIR = PROJECT_ROOT / "input"        # Raw downloaded videos land here
OUTPUT_DIR = PROJECT_ROOT / "output"      # Final short clips will be written here
TEMP_DIR = PROJECT_ROOT / "temp"          # Intermediate processing artefacts
LOGS_DIR = PROJECT_ROOT / "logs"          # Log files
STORAGE_PATH = PROJECT_ROOT / "storage"  # Per-user storage (Phase F)

# Ensure the core directories exist at import time
for _dir in (INPUT_DIR, OUTPUT_DIR, TEMP_DIR, LOGS_DIR, STORAGE_PATH):
    _dir.mkdir(parents=True, exist_ok=True)

# ── Pydantic BaseSettings (Phase A1) ──────────────────────────────────────────
try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
    from pydantic import Field, field_validator

    class Settings(BaseSettings):
        """Central settings loaded from .env — validates at startup."""

        # Phase A1 required keys
        MAX_VIDEO_DURATION_MINUTES: int = Field(default=90, ge=1, le=600, description="Max video duration minutes")
        MAX_FILE_SIZE_MB: int = Field(default=2000, ge=10, le=10000, description="Max file size MB")
        CLIPS_PER_VIDEO: int = Field(default=5, ge=1, le=20, description="Clips per video")
        CLIP_DURATION_MIN_SECONDS: int = Field(default=30, ge=5, le=60, description="Clip min seconds")
        CLIP_DURATION_MAX_SECONDS: int = Field(default=90, ge=10, le=300, description="Clip max seconds")
        CAPTION_STYLE: str = Field(default="karaoke", description="Caption style")
        FREE_TIER_MONTHLY_LIMIT: int = Field(default=5, ge=1, le=1000, description="Free tier monthly limit")
        JWT_SECRET_KEY: str = Field(default="", description="JWT secret")
        DATABASE_URL: str = Field(default="sqlite:///./data/users.db", description="Database URL")
        STORAGE_PATH: str = Field(default="./storage", description="Storage path")
        ASSEMBLYAI_API_KEY: str = Field(default="", description="AssemblyAI key")
        GOOGLE_API_KEY: str = Field(default="", description="Google key")
        VIDEOSAILOR_API_KEY: str = Field(default="", description="VideoSailor key")

        model_config = SettingsConfigDict(
            env_file=str(PROJECT_ROOT / ".env"),
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

        def validate_required(self):
            """Raise clear errors if critical keys missing — call at startup."""
            missing = []
            # These are critical for pipeline; warn but don't crash in dev
            for key in ["ASSEMBLYAI_API_KEY", "VIDEOSAILOR_API_KEY"]:
                if not getattr(self, key):
                    missing.append(key)
            if missing and os.environ.get("ENV", "development") == "production":
                raise ValueError(f"Missing required config keys: {', '.join(missing)} — set in .env or env vars")
            return missing

    settings = Settings()
    # Validate at import (warn in dev)
    _missing = settings.validate_required()
    if _missing:
        try:
            from src.logger import get_logger as _gl

            _gl("config").warning("Missing optional keys at startup: %s", ", ".join(_missing))
        except Exception:
            pass
except ImportError:
    # Fallback if pydantic-settings not installed
    settings = None  # type: ignore


# ══════════════════════════════════════════════════════════════════════════════
# 🔑 MASTER API KEYS & AI PROVIDERS CONFIGURATION (ALL-IN-ONE PLACE)
# ══════════════════════════════════════════════════════════════════════════════
# You can change all your API keys directly in the root `.env` file, in this
# section, or via the Web UI Settings panel (⚙️).
# ──────────────────────────────────────────────────────────────────────────────

# 1. Video Downloader (High-Speed YouTube API Downloader)
#    Get your API key at: https://videosailor.com/
VIDEOSAILOR_API_KEY = os.environ.get("VIDEOSAILOR_API_KEY", "").strip()

# 2. Transcription Provider & Key (AssemblyAI Cloud API)
#    Get AssemblyAI key at: https://www.assemblyai.com/dashboard/
TRANSCRIPTION_PROVIDER = "assemblyai"
ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "").strip()

# 3. AI Semantic Ranking Provider & Keys (Cloud LLMs)
#    Controls which AI brain scores and ranks the most viral moments (Phase 3.5).
#    Options: "gemini" (Google Flash - Recommended) | "openai" (GPT-4o-mini)
RANKING_PROVIDER = os.environ.get("RANKING_PROVIDER", "gemini").lower().strip()
GOOGLE_API_KEY   = os.environ.get("GOOGLE_API_KEY", "").strip()
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "").strip()

# ══════════════════════════════════════════════════════════════════════════════


def get_all_api_config() -> dict:
    """Return dictionary of configured provider status without exposing any keys."""
    return {
        "videosailor": {
            "is_set": bool(os.environ.get("VIDEOSAILOR_API_KEY", "").strip() or VIDEOSAILOR_API_KEY),
        },
        "assemblyai": {
            "is_set": bool(os.environ.get("ASSEMBLYAI_API_KEY", "").strip() or ASSEMBLYAI_API_KEY),
        },
        "google": {
            "is_set": bool(os.environ.get("GOOGLE_API_KEY", "").strip() or GOOGLE_API_KEY),
        },
        "openai": {
            "is_set": bool(os.environ.get("OPENAI_API_KEY", "").strip() or OPENAI_API_KEY),
        },
        "ranking_provider": os.environ.get("RANKING_PROVIDER", RANKING_PROVIDER),
        "transcription_provider": "assemblyai",
    }


def save_api_config(new_config: dict) -> None:
    """Save updated API keys and providers to .env and active runtime environment."""
    env_path = PROJECT_ROOT / ".env"
    env_lines: list[str] = []
    if env_path.exists():
        env_lines = env_path.read_text(encoding="utf-8").splitlines()

    def set_env_val(key: str, val: str):
        if val is None:
            return
        val_str = str(val).strip()
        os.environ[key] = val_str
        # Update global in this module
        if key in globals():
            globals()[key] = val_str

        # Update in-file representation
        found = False
        for i, line in enumerate(env_lines):
            if line.strip().startswith(f"{key}=") or line.strip().startswith(f"# {key}="):
                env_lines[i] = f"{key}={val_str}"
                found = True
                break
        if not found:
            env_lines.append(f"{key}={val_str}")

    if "VIDEOSAILOR_API_KEY" in new_config and new_config["VIDEOSAILOR_API_KEY"] is not None:
        set_env_val("VIDEOSAILOR_API_KEY", new_config["VIDEOSAILOR_API_KEY"])
    if "ASSEMBLYAI_API_KEY" in new_config and new_config["ASSEMBLYAI_API_KEY"] is not None:
        set_env_val("ASSEMBLYAI_API_KEY", new_config["ASSEMBLYAI_API_KEY"])
    if "GOOGLE_API_KEY" in new_config and new_config["GOOGLE_API_KEY"] is not None:
        set_env_val("GOOGLE_API_KEY", new_config["GOOGLE_API_KEY"])
    if "OPENAI_API_KEY" in new_config and new_config["OPENAI_API_KEY"] is not None:
        set_env_val("OPENAI_API_KEY", new_config["OPENAI_API_KEY"])
    if "RANKING_PROVIDER" in new_config and new_config["RANKING_PROVIDER"] is not None:
        set_env_val("RANKING_PROVIDER", new_config["RANKING_PROVIDER"])

    # Atomic write via temp file + replace to avoid race/corruption
    try:
        import tempfile

        # Validate no newline injection already done at API layer, double-check
        for line in env_lines:
            if "\n" in line or "\r" in line:
                raise ValueError("Invalid env line: newline injection")

        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(PROJECT_ROOT), text=True)
        try:
            with open(tmp_fd, "w", encoding="utf-8", newline="\n") as tmp:
                tmp.write("\n".join(env_lines) + "\n")
            # Atomic replace
            Path(tmp_path).replace(env_path)
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
    except Exception as err:
        from src.logger import get_logger as _get_logger

        _get_logger("config").error("Failed to write .env: %s", err)
        raise


# ── FFprobe / FFmpeg binary resolution ───────────────────────────────────────
# If ffmpeg/ffprobe are not on PATH, try to resolve from:
#   1. imageio_ffmpeg bundled binary (installed automatically as part of Phase 4)
#   2. System PATH as a final fallback
import shutil as _shutil

def _resolve_ffmpeg_bin() -> str:
    """Return the best available ffmpeg binary path."""
    if _shutil.which("ffmpeg"):
        return "ffmpeg"
    try:
        import imageio_ffmpeg as _iio_ffmpeg
        exe = _iio_ffmpeg.get_ffmpeg_exe()
        if exe:
            return exe
    except Exception:
        pass
    return "ffmpeg"

def _resolve_ffprobe_bin() -> str:
    """Return the best available ffprobe binary path."""
    if _shutil.which("ffprobe"):
        return "ffprobe"
    try:
        import imageio_ffmpeg as _iio_ffmpeg
        import pathlib as _pathlib
        ffmpeg_exe = _pathlib.Path(_iio_ffmpeg.get_ffmpeg_exe())
        ffprobe_candidate = ffmpeg_exe.parent / ffmpeg_exe.name.replace("ffmpeg", "ffprobe")
        if ffprobe_candidate.exists():
            return str(ffprobe_candidate)
        return str(ffmpeg_exe)
    except Exception:
        pass
    return "ffprobe"

FFMPEG_BIN = _resolve_ffmpeg_bin()
FFPROBE_BIN = _resolve_ffprobe_bin()


# ── Shorts Target Spec (used from Phase 3 onward) ─────────────────────────────
TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
TARGET_FPS = 30
MAX_SHORT_DURATION = 60   # seconds
MIN_SHORT_DURATION = 15   # seconds

# Intermediate audio file written to temp/ during extraction
AUDIO_TEMP_FILENAME = "extracted_audio.wav"

# Transcript output filenames (both written under TEMP_DIR)
TRANSCRIPT_JSON_FILENAME = "transcript.json"
TRANSCRIPT_TXT_FILENAME = "transcript.txt"

# ── Phase 3: Clip Selection ───────────────────────────────────────────────────

# Hard duration bounds for candidate clips (seconds)
CLIP_MIN_DURATION = 15.0
CLIP_MAX_DURATION = 20.0

# How many top candidates to select in the final output
CLIP_TOP_N = 30

# Minimum heuristic score required for a candidate to qualify for final selection
CLIP_MIN_SCORE = 30.0

# Minimum time separation (seconds) between selected clips on the podcast timeline.
# Prevents clustering (e.g. 5 clips from the same 3 minutes).
CLIP_MIN_SEPARATION = 90.0

# Timeline distribution strategy:
#   "spaced_top" — Global top scores with minimum distance suppression across timeline
#   "bucketed"   — Divide the podcast into N time buckets and select best candidate per bucket
CLIP_DISTRIBUTION_STRATEGY = "spaced_top"

# Sliding-window step size (seconds) — how far each candidate window advances.
# Smaller = generates more raw candidates; 1.5s - 2.0s ensures high candidate volume.
CLIP_STEP_SIZE = 1.5

# Overlap removal: two candidates are considered duplicates if their
# temporal overlap as a fraction of the shorter clip exceeds this threshold.
CLIP_OVERLAP_THRESHOLD = 0.4   # 40% overlap threshold for candidate deduplication

# Output filenames (written under TEMP_DIR)
CANDIDATE_POOL_JSON_FILENAME = "candidate_pool.json"  # Complete deduplicated pool (~180+ clips)
CANDIDATES_JSON_FILENAME = "candidates.json"          # Heuristic top-N (backwards compatibility)
CANDIDATES_TXT_FILENAME  = "candidates.txt"

# ── Configurable Scoring Weights ──────────────────────────────────────────────
SCORING_WEIGHTS: dict[str, float] = {
    # ── Hook Signals (Opening 5-10 words) ──────────────────────────────────
    "hook_question":            22.0,   # Opens with a strong question (Why/How/What if...)
    "hook_bold_claim":          18.0,   # Opens with strong statement / claim / opinion
    "hook_surprising":          18.0,   # Opens with surprising/unusual statement (Nobody knows...)
    "hook_formulaic_power":     16.0,   # "The biggest/most important/reason/problem/truth..."
    "hook_imperative":          15.0,   # Direct call / imperative (Think about, Imagine, Look at...)
    "hook_story_intro":         14.0,   # Story starter (I remember, What happened was, Back in...)
    "hook_number_stat":         12.0,   # Opens with a concrete number / statistic

    # ── Body Signals (Engagement, Flow & Value) ───────────────────────────
    "body_question":             8.0,   # Mid-clip question creating curiosity tension
    "body_contrast":             8.0,   # Contrast words (but, however, instead, because)
    "body_educational":          8.0,   # Explanatory value (the reason, this means, in other words)
    "body_statistics":           7.0,   # Data / numbers in body supporting the point
    "body_storytelling":         6.0,   # Narrative flow indicators (and then, at that point...)
    "body_emotional":            6.0,   # Emotional / high-intensity adjectives and verbs
    "body_surprise":             6.0,   # Shocking / unexpected revelation mid-clip

    # ── Standalone & Completeness Signals ─────────────────────────────────
    "standalone_subject_intro": 12.0,   # Introduces clear subject/entity before using pronouns
    "complete_thought":         12.0,   # Clean finish with terminal sentence punctuation
    "clean_sentence_start":      8.0,   # Starts cleanly at the true beginning of a sentence
    "high_confidence":           5.0,   # Whisper avg_logprob > -0.12 (clear audio)
    "good_pacing":               4.0,   # Speaking rate in optimal 2.0 - 3.4 words/sec range

    # ── Penalties ─────────────────────────────────────────────────────────
    "penalty_dangling_open":   -25.0,   # Starts with dependent conjunction / preposition (from/which/that/because...)
    "penalty_weak_filler_open":-20.0,   # Starts with filler (yeah/well/okay/you know/um/uh/and/so...)
    "penalty_unresolved_pronoun":-18.0, # Starts with pronoun (he/she/they/it/this) without local antecedent
    "penalty_incomplete_end":  -15.0,   # Cuts off mid-sentence without terminal punctuation
    "penalty_context_dependent":-15.0,  # Highly dependent on external dialogue/context
    "penalty_filler_dense":    -10.0,   # High proportion of filler words (> 15%)
    "penalty_low_confidence":   -8.0,   # Whisper avg_logprob < -0.22 (mumbled/noisy)
    "penalty_sparse_words":     -8.0,   # Very few words (< 22 words in 15-20s)
}

# ── Phase 3.5: Local LLM Semantic Ranking ─────────────────────────────────────
OLLAMA_BASE_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
OLLAMA_TIMEOUT = 60  # seconds per candidate request

# Pre-ranking semantic pool size (fast local heuristic ranking before sending to LLM)
SEMANTIC_DEFAULT_POOL_SIZE = 100

# Boundary refinement target duration for semantic ranking
SEMANTIC_REFINE_MIN_DURATION = CLIP_MIN_DURATION   # 15.0
SEMANTIC_REFINE_MAX_DURATION = CLIP_MAX_DURATION   # 20.0

SEMANTIC_MIN_SCORE = 40.0
SEMANTIC_DEFAULT_TOP_N = 30
SEMANTIC_DEFAULT_SEPARATION = 90.0

SEMANTIC_JSON_FILENAME = "semantic_candidates.json"
SEMANTIC_TXT_FILENAME  = "semantic_candidates.txt"

# ── Phase 5: Caption Settings ────────────────────────────────────────────────
CAPTION_FONT_SIZE = 82
CAPTION_MAX_WORDS = 5
CAPTION_MIN_WORDS = 2
CAPTION_MAX_LINES = 2
CAPTION_MAX_WIDTH = 900
CAPTION_Y = 1450

# Colors (R, G, B)
CAPTION_TEXT_COLOR = (255, 255, 255)         # White
CAPTION_HIGHLIGHT_COLOR = (255, 230, 0)      # Yellow
CAPTION_OUTLINE_COLOR = (0, 0, 0)            # Black
CAPTION_OUTLINE_WIDTH = 6

CAPTION_START_PADDING = 0.0
CAPTION_END_PADDING = 0.05

CAPTION_MAX_DURATION = 2.5
CAPTION_MIN_DURATION = 0.4

# ── Automatic Video Enhancement & Audio Polish Settings ──────────────────────
# Subtle color vibrance + contrast + light unsharp sharpening applied automatically to all shorts
AUTO_COLOR_FILTER_ENABLED = True
AUTO_VIDEO_FILTER = "eq=contrast=1.06:saturation=1.12:brightness=0.01,unsharp=5:5:0.35:3:3:0.0"

# Subtle audio pitch adjustment (+0.5 semitones) for crisp, punchy voice presence
AUTO_PITCH_SHIFT_ENABLED = True
AUTO_PITCH_SEMITONES = 0.5



