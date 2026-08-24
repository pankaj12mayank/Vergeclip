"""
app/transcriber.py
------------------
Phase 2 module: Lightning-fast multilingual audio transcription using
AssemblyAI Cloud API with word-level timestamps and speaker diarization.

Design goals
────────────
• 100% Cloud-Native & Production Ready: No local ML models, no heavy PyTorch/CTranslate2 dependencies.
• Fast & Memory-Efficient: Audio is extracted to a lightweight MP3 (~10-20 MB for 2hr audio)
  and uploaded to AssemblyAI in seconds.
• High Accuracy: Word-level timestamps, automatic punctuation, and natural speaker chunking.
• Handles non-spoken clips: music/action with no dialogue.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.config import (
    AUDIO_TEMP_FILENAME,
    FFMPEG_BIN,
    INPUT_DIR,
    TEMP_DIR,
    TRANSCRIPT_JSON_FILENAME,
    TRANSCRIPT_TXT_FILENAME,
)
from src.logger import get_logger

log = get_logger(__name__)

# ── Video file extensions that we accept ──────────────────────────────────────
_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".flv"}


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class Segment:
    """One transcript segment with millisecond-accurate timestamps."""
    id: int
    start: float          # seconds (float, e.g. 12.34)
    end: float            # seconds
    text: str
    avg_logprob: float = 0.0    # confidence proxy
    no_speech_prob: float = 0.0 # probability that the segment is silence/noise
    words: list[dict] = field(default_factory=list)  # word-level timestamps


@dataclass
class TranscriptResult:
    """Complete transcription output for one video."""
    video_file: str           # basename of the source video
    model: str
    device: str
    compute_type: str
    language: Optional[str]   # detected or forced language code
    duration_secs: float
    generated_at: str         # ISO-8601 UTC timestamp
    segments: list[Segment] = field(default_factory=list)

    @property
    def num_segments(self) -> int:
        return len(self.segments)

    @property
    def full_text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["num_segments"] = self.num_segments
        return d


# ── File loading helpers ──────────────────────────────────────────────────────

def load_latest_video(input_dir: Path = INPUT_DIR) -> Path:
    """
    Return the most-recently-modified video file in *input_dir*.

    Raises:
        FileNotFoundError: If no video files exist in *input_dir*.
    """
    if not input_dir.exists():
        raise FileNotFoundError(
            f"Input directory does not exist: {input_dir}\n"
            "Run Phase 1 (downloader) first or place a video in input/."
        )

    candidates: list[Path] = [
        f for f in input_dir.iterdir()
        if f.is_file() and f.suffix.lower() in _VIDEO_EXTENSIONS
    ]

    if not candidates:
        raise FileNotFoundError(
            f"No video files ({', '.join(_VIDEO_EXTENSIONS)}) found in: {input_dir}\n"
            "Please download or place a video file there first."
        )

    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    log.info("Loaded latest video: %s (%.1f MB)", latest.name, latest.stat().st_size / 1e6)
    return latest


# ── Audio extraction ───────────────────────────────────────────────────────────

def extract_audio(
    video_path: Path,
    output_audio_path: Path,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
) -> Path:
    """
    Extract a fast, lightweight audio track from *video_path* via ffmpeg.
    """
    output_audio_path.parent.mkdir(parents=True, exist_ok=True)

    # If extracting to MP3, use libmp3lame with 64k bitrate for rapid cloud upload
    if output_audio_path.suffix.lower() == ".mp3":
        cmd = [
            FFMPEG_BIN, "-y",
            "-i", str(video_path),
            "-vn",
            "-acodec", "libmp3lame",
            "-b:a", "64k",
            "-ar", str(sample_rate),
            "-ac", str(channels),
            str(output_audio_path),
        ]
    else:
        cmd = [
            FFMPEG_BIN, "-y",
            "-i", str(video_path),
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", str(sample_rate),
            "-ac", str(channels),
            str(output_audio_path),
        ]

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if proc.returncode != 0:
        # Fallback without specific codec
        fallback_cmd = [
            FFMPEG_BIN, "-y",
            "-i", str(video_path),
            "-vn",
            str(output_audio_path),
        ]
        proc = subprocess.run(fallback_cmd, capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg audio extraction failed:\n{proc.stderr}")

    return output_audio_path


# ── AssemblyAI Cloud Transcription ─────────────────────────────────────────────

def _rechunk_by_words(
    segments: list[Segment],
    target_dur: float = 5.0,
    max_words: int = 20,
) -> list[Segment]:
    """Split long monolithic segments into natural 3-8s phrase chunks using word timestamps."""
    all_words: list[dict] = []
    for s in segments:
        all_words.extend(s.words)

    if not all_words:
        return segments

    chunks: list[Segment] = []
    curr_words: list[dict] = []
    seg_idx = 0

    for w in all_words:
        curr_words.append(w)
        dur = curr_words[-1]["end"] - curr_words[0]["start"]
        word_text = str(w.get("word", "")).strip()
        is_terminal = word_text.endswith((".", "?", "!", ";", "..."))

        if (dur >= target_dur and is_terminal) or dur >= target_dur * 1.8 or len(curr_words) >= max_words:
            chunks.append(Segment(
                id=seg_idx,
                start=curr_words[0]["start"],
                end=curr_words[-1]["end"],
                text=" ".join(str(item.get("word", "")).strip() for item in curr_words),
                avg_logprob=0.0,
                no_speech_prob=0.0,
                words=curr_words,
            ))
            seg_idx += 1
            curr_words = []

    if curr_words:
        chunks.append(Segment(
            id=seg_idx,
            start=curr_words[0]["start"],
            end=curr_words[-1]["end"],
            text=" ".join(str(item.get("word", "")).strip() for item in curr_words),
            avg_logprob=0.0,
            no_speech_prob=0.0,
            words=curr_words,
        ))

    return chunks


def transcribe_with_assemblyai(
    audio_path: Path,
    api_key: Optional[str] = None,
    language_code: Optional[str] = None,
) -> tuple[list[Segment], str]:
    """
    Transcribe audio via AssemblyAI Cloud API (~15-30s) with speaker & word-level timestamps.
    """
    from src.config import ASSEMBLYAI_API_KEY
    key = (api_key or os.environ.get("ASSEMBLYAI_API_KEY", "") or ASSEMBLYAI_API_KEY).strip()
    if not key:
        raise ValueError("No AssemblyAI API key provided. Set ASSEMBLYAI_API_KEY in environment or .env.")

    if not audio_path.exists() or audio_path.stat().st_size == 0:
        raise FileNotFoundError(f"Audio file for transcription not found or empty: {audio_path}")

    import assemblyai as aai

    aai.settings.api_key = key
    log.info("Transcribing audio via AssemblyAI Cloud API (%s)...", audio_path.name)

    transcriber = aai.Transcriber()
    config_kwargs = {
        "speaker_labels": True,
        "punctuate": True,
        "format_text": True,
    }
    if language_code:
        config_kwargs["language_code"] = language_code
    else:
        config_kwargs["language_detection"] = True

    config = aai.TranscriptionConfig(**config_kwargs)
    try:
        transcript = transcriber.transcribe(str(audio_path), config=config)
    except Exception as trans_err:
        if "language_detection" in str(trans_err).lower() and not language_code:
            log.warning("AssemblyAI language detection reported no spoken audio. Retrying with standard English config...")
            fallback_config = aai.TranscriptionConfig(
                speaker_labels=True,
                punctuate=True,
                format_text=True,
                language_code="en",
            )
            transcript = transcriber.transcribe(str(audio_path), config=fallback_config)
        else:
            raise trans_err

    if transcript.status == aai.TranscriptStatus.error:
        err_msg = str(transcript.error or "Unknown AssemblyAI error")
        if "no spoken audio" in err_msg.lower() or "language_detection" in err_msg.lower():
            log.info("AssemblyAI detected no spoken speech in audio file.")
            return [], "en"
        raise RuntimeError(f"AssemblyAI Error: {err_msg}")

    segments: list[Segment] = []

    # Use utterances for natural conversational segment boundaries
    if transcript.utterances:
        for idx, u in enumerate(transcript.utterances):
            u_words = []
            for w in u.words:
                u_words.append({
                    "word": w.text,
                    "start": round(w.start / 1000.0, 3),
                    "end": round(w.end / 1000.0, 3),
                    "probability": round(float(w.confidence or 1.0), 3),
                })
            segments.append(Segment(
                id=idx,
                start=round(u.start / 1000.0, 3),
                end=round(u.end / 1000.0, 3),
                text=u.text,
                avg_logprob=0.0,
                no_speech_prob=0.0,
                words=u_words,
            ))
    elif transcript.words:
        curr_words = []
        seg_id = 0
        for w in transcript.words:
            w_dict = {
                "word": w.text,
                "start": round(w.start / 1000.0, 3),
                "end": round(w.end / 1000.0, 3),
                "probability": round(float(w.confidence or 1.0), 3),
            }
            curr_words.append(w_dict)
            if w.text.endswith((".", "?", "!", ";")) or len(curr_words) >= 15:
                s_start = curr_words[0]["start"]
                s_end = curr_words[-1]["end"]
                s_text = " ".join(item["word"] for item in curr_words)
                segments.append(Segment(
                    id=seg_id,
                    start=s_start,
                    end=s_end,
                    text=s_text,
                    avg_logprob=0.0,
                    no_speech_prob=0.0,
                    words=curr_words,
                ))
                seg_id += 1
                curr_words = []
        if curr_words:
            segments.append(Segment(
                id=seg_id,
                start=curr_words[0]["start"],
                end=curr_words[-1]["end"],
                text=" ".join(item["word"] for item in curr_words),
                avg_logprob=0.0,
                no_speech_prob=0.0,
                words=curr_words,
            ))

    detected_language = "en"
    if hasattr(transcript, "json_response") and isinstance(transcript.json_response, dict):
        detected_language = transcript.json_response.get("language_code", "en")

    # Re-chunk monolithic segments if necessary
    if len(segments) <= 1 and segments:
        segments = _rechunk_by_words(segments, target_dur=5.0, max_words=20)
        log.info("Re-chunked monolithic segment into %d sentence segments", len(segments))

    return segments, detected_language


def transcribe_video(
    video_path: Optional[Path] = None,
    *,
    provider: Optional[str] = "assemblyai",
    model_name: Optional[str] = None,
    language: Optional[str] = None,
    keep_audio: bool = False,
    json_path: Optional[Path] = None,
    txt_path: Optional[Path] = None,
) -> TranscriptResult:
    """
    Full Phase 2 pipeline: extract audio -> transcribe with AssemblyAI -> save transcripts.
    """
    from src.config import ASSEMBLYAI_API_KEY

    # ── Resolve video ──────────────────────────────────────────────────────────
    if video_path is None:
        video_path = load_latest_video()

    # ── Extract audio ──────────────────────────────────────────────────────────
    audio_path = TEMP_DIR / "extracted_audio.mp3"
    extract_audio(video_path, audio_path)

    active_assembly_key = (os.environ.get("ASSEMBLYAI_API_KEY", "") or ASSEMBLYAI_API_KEY).strip()
    if not active_assembly_key:
        raise ValueError(
            "ASSEMBLYAI_API_KEY is not set. Please provide ASSEMBLYAI_API_KEY in your environment or .env file."
        )

    log.info("🚀 Starting AssemblyAI Cloud transcription for '%s'...", video_path.name)
    segments, detected_lang = transcribe_with_assemblyai(
        audio_path=audio_path,
        api_key=active_assembly_key,
        language_code=language,
    )

    total_media_dur = _get_video_file_duration(video_path)
    final_duration = segments[-1].end if segments else total_media_dur
    if final_duration <= 0.0 and total_media_dur > 0.0:
        final_duration = total_media_dur

    if not segments:
        log.info("No spoken dialogue detected by AssemblyAI — Visual highlight engine will score scene energy.")

    result = TranscriptResult(
        video_file=video_path.name,
        model="assemblyai-universal",
        device="cloud",
        compute_type="api",
        language=detected_lang,
        duration_secs=round(final_duration, 3),
        generated_at=datetime.now(timezone.utc).isoformat(),
        segments=segments,
    )

    log.info(
        "✓ Transcription complete: %d segments, %.1f min speech (media duration %.1f min).",
        result.num_segments,
        (segments[-1].end / 60) if segments else 0.0,
        final_duration / 60,
    )

    # ── Save outputs ───────────────────────────────────────────────────────────
    _save_json(result, json_path or (TEMP_DIR / TRANSCRIPT_JSON_FILENAME))
    _save_txt(result,  txt_path  or (TEMP_DIR / TRANSCRIPT_TXT_FILENAME))

    # ── Cleanup ────────────────────────────────────────────────────────────────
    if not keep_audio and audio_path.exists():
        try:
            audio_path.unlink(missing_ok=True)
        except Exception:
            pass

    return result


def _get_video_file_duration(video_path: Path) -> float:
    """Return duration of video file in seconds."""
    try:
        from src.inspector import inspect_video
        info = inspect_video(video_path)
        if info.duration_secs > 0:
            return round(info.duration_secs, 3)
    except Exception:
        pass
    try:
        import cv2
        cap = cv2.VideoCapture(str(video_path))
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frames = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
        cap.release()
        if fps > 0 and frames > 0:
            return round(frames / fps, 3)
    except Exception:
        pass
    return 0.0


def _save_json(result: TranscriptResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2, ensure_ascii=False)
    log.info("Saved transcript JSON -> %s", path.name)


def _save_txt(result: TranscriptResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for s in result.segments:
        m, sec = divmod(s.start, 60)
        h, m = divmod(m, 60)
        ts = f"{int(h):02d}:{int(m):02d}:{sec:06.3f}"
        lines.append(f"[{ts}] {s.text.strip()}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("Saved transcript TXT -> %s", path.name)
