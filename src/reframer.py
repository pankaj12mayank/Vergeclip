"""
src/reframer.py
----------------
Phase 4.5, 4.6 & 4.7 — Compute per-frame 9:16 (1080×1920) crop windows
from face tracking data, smooth camera movement using exponential
weighted moving average, and write the reframed video to disk via FFmpeg.

Public API:
    compute_crop_plan(frame_data, src_w, src_h, clip_text, segments) -> list[CropWindow]
    render_reframed_video(source_clip, crop_plan, out_path, fps) -> Path
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from src.config import FFMPEG_BIN, get_video_spec_config
from src.face_tracker import FaceBox, FrameData
from src.logger import get_logger

log = get_logger(__name__)

# ─── Reframing constants (loaded dynamically from DB/settings) ─────────────────

def _get_out_w():
    return get_video_spec_config()["target_width"]

def _get_out_h():
    return get_video_spec_config()["target_height"]

def _get_aspect():
    return _get_out_w() / _get_out_h()

# Camera smoothing — lower alpha = smoother but laggier
SMOOTH_ALPHA = 0.08         # Very smooth: matches a slow broadcast PTZ feel

# Safe zone: face centroid target position within the output frame (top 40%)
FACE_TARGET_Y_FRAC = 0.38   # Face center sits at 38% from top (leaves caption space)
FACE_TARGET_X_FRAC = 0.50   # Horizontally centered

# Padding above face for forehead clearance
FACE_PADDING_TOP_FRAC = 0.15  # Add 15% of face height as top padding
FACE_PADDING_SIDE_FRAC = 0.25  # Horizontal padding fraction of face width


@dataclass
class CropWindow:
    """A (x, y, w, h) crop in source-video coordinates for one frame."""
    frame_idx: int
    timestamp: float
    x: int
    y: int
    w: int
    h: int

    def to_dict(self) -> dict:
        return {
            "frame_idx": self.frame_idx,
            "timestamp": round(self.timestamp, 4),
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
        }


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _compute_target_crop(
    face: Optional[FaceBox],
    src_w: int,
    src_h: int,
) -> tuple[float, float, float, float]:
    """
    Compute the ideal (cx, cy, crop_w, crop_h) in source pixel coordinates
    for a single frame given the primary face (or None for center crop).
    """
    # Crop dimensions to fill 9:16 at full source height
    crop_h = float(src_h)
    crop_w = crop_h * _get_aspect()

    # If crop_w > src_w, scale down to fit
    if crop_w > src_w:
        crop_w = float(src_w)
        crop_h = crop_w / _get_aspect()

    if face is None:
        # Fallback: center crop
        cx = src_w / 2.0
        cy = src_h / 2.0
        return cx, cy, crop_w, crop_h

    # Place face center at FACE_TARGET_Y_FRAC from top of crop window
    # face_center_in_output_y = FACE_TARGET_Y_FRAC * crop_h
    # => output_top = face.cy - face_center_in_output_y
    # => crop center_y = face.cy - face_center_in_output_y + crop_h/2

    face_cy = face.cy
    output_face_y = FACE_TARGET_Y_FRAC * crop_h
    desired_cy = face_cy - output_face_y + crop_h / 2.0

    # Horizontally: keep face center near middle of frame
    desired_cx = face.cx  # Follow face horizontally

    # Clamp so crop stays within frame
    half_w = crop_w / 2.0
    half_h = crop_h / 2.0
    cx = _clamp(desired_cx, half_w, src_w - half_w)
    cy = _clamp(desired_cy, half_h, src_h - half_h)

    return cx, cy, crop_w, crop_h


def compute_crop_plan(
    frame_data: list[FrameData],
    src_w: int,
    src_h: int,
) -> list[CropWindow]:
    """
    Phase 4.6 — Generate a smoothed per-frame crop plan for 9:16 reframing.

    Uses exponential weighted moving average over (cx, cy, crop_w, crop_h)
    to eliminate jitter. Interpolates missing face frames from neighbors.
    """
    if not frame_data:
        return []

    # Pass 1: compute raw target crop per frame
    raw_targets: list[tuple[float, float, float, float]] = []
    for fd in frame_data:
        cx, cy, cw, ch = _compute_target_crop(fd.primary_face, src_w, src_h)
        raw_targets.append((cx, cy, cw, ch))

    # Pass 2: smooth with EWMA
    smoothed: list[tuple[float, float, float, float]] = []
    cx_s, cy_s, cw_s, ch_s = raw_targets[0]
    smoothed.append((cx_s, cy_s, cw_s, ch_s))

    a = SMOOTH_ALPHA
    for cx, cy, cw, ch in raw_targets[1:]:
        cx_s = a * cx + (1 - a) * cx_s
        cy_s = a * cy + (1 - a) * cy_s
        cw_s = a * cw + (1 - a) * cw_s
        ch_s = a * ch + (1 - a) * ch_s
        smoothed.append((cx_s, cy_s, cw_s, ch_s))

    # Pass 3: convert (cx,cy,w,h) -> (x,y,w,h) top-left, clamped
    crop_plan: list[CropWindow] = []
    for fd, (cx, cy, cw, ch) in zip(frame_data, smoothed):
        x = int(_clamp(cx - cw / 2, 0, src_w - cw))
        y = int(_clamp(cy - ch / 2, 0, src_h - ch))
        w = int(cw)
        h = int(ch)
        crop_plan.append(CropWindow(
            frame_idx=fd.frame_idx,
            timestamp=fd.timestamp,
            x=x,
            y=y,
            w=w,
            h=h,
        ))

    log.info(
        "Crop plan ready: %d frames, crop=%dx%d -> output %dx%d",
        len(crop_plan),
        crop_plan[0].w, crop_plan[0].h,
        _get_out_w(), _get_out_h(),
    )
    return crop_plan


def _write_crop_script(
    crop_plan: list[CropWindow],
    fps: float,
    script_path: Path,
) -> None:
    """
    Write an FFmpeg sendcmd / expression script using dynamic lavfi crop filters.
    Since FFmpeg's crop filter can accept expressions based on time, we write
    one keyframe per frame as a crop= override using a custom filter_complex.

    Strategy: use a Python-written per-frame crop as an FFmpeg zoompan/crop
    script via the 'cropdetect' approach, or use FFmpeg's 'select' + 'setpts'.

    Simpler approach: we'll precompute and write per-frame crop data, then
    drive it through FFmpeg using the expressions t-based interpolation approach.
    We write the x:y:w:h for every frame into a sendcmd file.
    """
    lines = []
    for cw in crop_plan:
        # sendcmd format: <time> [enter|leave] <filter> <cmd_name> <cmd_value>;
        # But crop doesn't support sendcmd well. Instead we write an
        # ffmpeg script-friendly expression by using the 'crop' filter
        # with per-second keyframing (close enough for smoothed trajectories).
        # We'll write frame-level data as a CSV and use Python to apply it.
        t = cw.timestamp
        lines.append(f"{t:.4f},{cw.x},{cw.y},{cw.w},{cw.h}")

    with script_path.open("w") as fh:
        fh.write("\n".join(lines))


def render_reframed_video(
    source_clip: Path,
    crop_plan: list[CropWindow],
    out_path: Path,
    fps: float = 30.0,
    audio_path: Optional[Path] = None,
) -> Path:
    """
    Phase 4.7 — Render the 9:16 reframed video using OpenCV for frame-level
    crop application, then encode with FFmpeg.

    Process:
      1. Read source_clip frame-by-frame with OpenCV.
      2. Apply the per-frame crop from crop_plan.
      3. Resize each cropped frame to OUT_W × OUT_H.
      4. Write frames to a temporary raw video.
      5. Mux with original audio using FFmpeg.
    """
    import cv2

    if not source_clip.exists():
        raise FileNotFoundError(f"Source clip not found: {source_clip}")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Open source video
    cap = cv2.VideoCapture(str(source_clip))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {source_clip}")

    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or fps
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # Build an index of crop_plan by frame_idx for O(1) lookup
    crop_by_frame: dict[int, CropWindow] = {c.frame_idx: c for c in crop_plan}

    # We'll write to a temp raw video (no audio) then mux audio
    tmp_raw = out_path.with_suffix("") / Path("_reframed_noaudio.mp4")
    tmp_raw = out_path.parent / "_reframed_noaudio.mp4"

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(tmp_raw), fourcc, src_fps, (_get_out_w(), _get_out_h()))

    if not writer.isOpened():
        cap.release()
        raise RuntimeError("Could not open VideoWriter. Check OpenCV codec support.")

    log.info(
        "Reframing %dx%d -> %dx%d @ %.2ffps (%d frames)…",
        src_w, src_h, _get_out_w(), _get_out_h(), src_fps, total_frames
    )

    last_crop = crop_plan[0] if crop_plan else CropWindow(0, 0, 0, 0, src_w, src_h)

    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        cw = crop_by_frame.get(frame_idx, last_crop)
        last_crop = cw

        # Apply crop — clamp to frame bounds defensively
        x = max(0, min(cw.x, src_w - 1))
        y = max(0, min(cw.y, src_h - 1))
        w = max(1, min(cw.w, src_w - x))
        h = max(1, min(cw.h, src_h - y))

        cropped = frame[y:y + h, x:x + w]

        if cropped.size == 0:
            # Fallback: center crop
            cx = src_w // 2
            cy = src_h // 2
            cw_ = min(src_w, int(src_h * _get_aspect()))
            ch_ = min(src_h, int(cw_ / _get_aspect()))
            cropped = frame[
                max(0, cy - ch_ // 2): min(src_h, cy + ch_ // 2),
                max(0, cx - cw_ // 2): min(src_w, cx + cw_ // 2),
            ]

        resized = cv2.resize(cropped, (_get_out_w(), _get_out_h()), interpolation=cv2.INTER_LINEAR)
        writer.write(resized)
        frame_idx += 1

    cap.release()
    writer.release()
    import gc
    gc.collect()
    log.info("Raw reframed frames written -> %s", tmp_raw)

    # Mux: combine raw video with original audio
    audio_src = audio_path or source_clip
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-threads", "0",         # Use all available CPU threads
        "-r", str(src_fps),
        "-i", str(tmp_raw),
        "-i", str(audio_src),
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "ultrafast",  # Fastest encoding preset
        "-tune", "fastdecode",
        "-pix_fmt", "yuv420p",
        "-r", str(src_fps),
        "-c:a", "aac",
        "-b:a", "192k",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
        str(out_path),
    ]
    log.info("Encoding reframed video with FFmpeg…")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg mux failed.\nCommand: {' '.join(cmd)}\nStderr:\n{result.stderr}"
        )

    # Clean up temp raw video
    try:
        tmp_raw.unlink()
    except Exception:
        pass

    log.info("Reframed video saved -> %s", out_path)
    return out_path
