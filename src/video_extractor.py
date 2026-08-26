"""
src/video_extractor.py
-----------------------
Phase 4.1 & 4.2 — Select a candidate clip from semantic_candidates.json,
apply intelligent 15-20 second trim if needed, then extract the exact
segment from the source video using FFmpeg accurate seeking.

Public API:
    select_and_extract(rank, candidates_path, input_video_path, out_path) -> ClipInfo
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.config import (
    FFMPEG_BIN,
    FFPROBE_BIN,
    SEMANTIC_JSON_FILENAME,
    TEMP_DIR,
)
from src.logger import get_logger

log = get_logger(__name__)

# Target duration bounds for final output
OUTPUT_MIN_DUR = 15.0
OUTPUT_MAX_DUR = 20.0


@dataclass
class ClipInfo:
    """Metadata for the selected and extracted clip."""
    rank: int
    start: float
    end: float
    duration: float
    text: str
    source_video: Path
    extracted_clip: Path
    was_trimmed: bool
    original_duration: float


def _find_input_video() -> Path:
    """Find the most recently modified video in input/ directory."""
    from src.config import INPUT_DIR
    valid_exts = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
    candidates = sorted(
        [f for f in INPUT_DIR.glob("*") if f.suffix.lower() in valid_exts and f.is_file()],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            "No video found in input/. "
            "Run Phase 1 first: python -m app.main download <url>"
        )
    return candidates[0]


def _fmt_ts(secs: float) -> str:
    total = int(secs)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    ms = int((secs - int(secs)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _trim_to_duration(
    text: str,
    segments: list[dict],
    start: float,
    end: float,
) -> tuple[float, float, str, bool]:
    """
    If the clip exceeds OUTPUT_MAX_DUR seconds, intelligently trim it by:
    1. Looking for the last sentence-ending punctuation within the 20s window.
    2. If none found, cut at exactly OUTPUT_MAX_DUR (non-sentence-aware fallback).

    Returns (new_start, new_end, new_text, was_trimmed).
    """
    duration = end - start
    if duration <= OUTPUT_MAX_DUR:
        return start, end, text, False

    # Find segments within the clip
    clip_segs = [
        s for s in segments
        if float(s.get("start", 0)) >= start - 0.2
        and float(s.get("end", 0)) <= start + OUTPUT_MAX_DUR + 0.5
    ]

    # Walk segments forward and find the latest sentence boundary ≤ 20s
    best_end = None
    best_text = None
    accumulated = []

    for seg in clip_segs:
        seg_end = float(seg["end"])
        if seg_end - start > OUTPUT_MAX_DUR:
            break
        accumulated.append(seg["text"].strip())
        seg_text = seg["text"].strip()
        # Check for sentence-ending punctuation
        if re.search(r"[.!?]['\"]?\s*$", seg_text):
            best_end = seg_end
            best_text = " ".join(accumulated)

    if best_end is not None:
        log.info(
            "Trimmed clip from %.1fs -> %.1fs at sentence boundary",
            duration,
            best_end - start,
        )
        return start, best_end, best_text, True

    # Fallback: hard cut at OUTPUT_MAX_DUR
    hard_end = start + OUTPUT_MAX_DUR
    log.warning(
        "No sentence boundary found within %.1fs; hard cut at %.1fs",
        OUTPUT_MAX_DUR,
        OUTPUT_MAX_DUR,
    )
    # Reconstruct text from segments up to hard_end
    fallback_segs = [
        s["text"].strip()
        for s in clip_segs
        if float(s["end"]) <= hard_end
    ]
    fallback_text = " ".join(fallback_segs)
    return start, hard_end, fallback_text or text[: len(text) // 2], True


def _run_ffmpeg(cmd: list[str]) -> None:
    """Run FFmpeg command, raise on failure with full output."""
    log.debug("FFmpeg: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg failed (code {result.returncode}).\n"
            f"Command: {' '.join(cmd)}\n"
            f"Stderr:\n{result.stderr}"
        )


def _get_video_fps(video_path: Path) -> float:
    """Read the frame rate of the video using OpenCV or ffprobe."""
    import cv2
    cap = cv2.VideoCapture(str(video_path))
    if cap.isOpened():
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        cap.release()
        if fps > 0:
            return round(fps, 3)

    return 25.0


def extract_clip(
    source_video: Path,
    start: float,
    end: float,
    out_path: Path,
) -> None:
    """
    Extract a precise time segment from source_video using FFmpeg.
    Uses two-stage seeking (coarse keyframe seek + fine decode seek) to guarantee
    exact millisecond audio/video alignment with transcript timestamps.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    duration = max(1.0, end - start)

    # 2-stage seek: coarse jump to 10s before, then fine decode seek to exact start
    pre_seek = max(0.0, start - 10.0)
    in_seek = start - pre_seek

    cmd = [
        FFMPEG_BIN,
        "-y",
        "-ss", f"{pre_seek:.3f}",        # fast container seek to nearby keyframe
        "-i", str(source_video),
        "-ss", f"{in_seek:.3f}",         # exact frame/audio decode seek
        "-t", f"{duration:.3f}",         # exact cut duration
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "ultrafast",
        "-c:a", "aac",
        "-b:a", "192k",
        "-avoid_negative_ts", "make_zero",
        str(out_path),
    ]
    log.info("Extracting clip: %s -> %s (%.2fs)", _fmt_ts(start), _fmt_ts(end), duration)
    _run_ffmpeg(cmd)
    log.info("Saved extracted clip -> %s", out_path)


def select_and_extract(
    rank: int = 1,
    candidates_path: Optional[Path] = None,
    input_video_path: Optional[Path] = None,
    out_path: Optional[Path] = None,
    transcript_path: Optional[Path] = None,
    quiet: bool = False,
) -> ClipInfo:
    """
    Phase 4.1 + 4.2 entry point.

    1. Reads semantic_candidates.json.
    2. Selects the clip at the given rank (1-indexed).
    3. Uses candidate start/end as exact source of truth.
    4. Extracts the frame-accurate clip segment from the source video using FFmpeg.
    5. Returns ClipInfo with all metadata.
    """
    # Resolve paths
    cand_path = candidates_path or (TEMP_DIR / SEMANTIC_JSON_FILENAME)
    if not cand_path.exists():
        raise FileNotFoundError(
            f"Semantic candidates file not found at {cand_path}.\n"
            "Run Phase 3.5 first: python -m app.main rank-clips"
        )

    with cand_path.open(encoding="utf-8") as fh:
        data = json.load(fh)

    candidates = data.get("candidates", [])
    if not candidates:
        raise ValueError("No candidates found in semantic_candidates.json")

    if rank < 1 or rank > len(candidates):
        raise ValueError(f"Rank {rank} out of range (1–{len(candidates)})")

    # Candidates are ordered by semantic score descending; rank 1 = index 0
    cand = candidates[rank - 1]

    c_start = float(cand["start"])
    c_end = float(cand["end"])
    c_duration = round(c_end - c_start, 3)
    c_text = cand.get("text", "")

    # Print selected clip info if not quiet
    if not quiet:
        print("\nTEST CLIP")
        print("-----------")
        print(f"Rank      : {rank}")
        print(f"Start     : {_fmt_ts(c_start)}")
        print(f"End       : {_fmt_ts(c_end)}")
        print(f"Duration  : {c_duration:.2f}s")
        print(f"Transcript: {c_text[:200]}{'...' if len(c_text) > 200 else ''}")
        print()

    # Find source video
    source_vid = input_video_path or _find_input_video()
    log.info("Source video: %s", source_vid.name)

    # Output path for raw extracted clip
    extract_out = out_path or (TEMP_DIR / "phase4" / "source_clip.mp4")

    # 4.2 — Extract exact clip
    extract_clip(source_vid, c_start, c_end, extract_out)

    return ClipInfo(
        rank=rank,
        start=c_start,
        end=c_end,
        duration=c_duration,
        text=c_text,
        source_video=source_vid,
        extracted_clip=extract_out,
        was_trimmed=False,
        original_duration=c_duration,
    )
