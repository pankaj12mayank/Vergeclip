"""
src/scene_fallback.py
---------------------
3-Tier Visual Fallback Engine for Script-to-Video.

Resolves segment visuals gracefully so script_video_renderer NEVER hard-fails:
  - Tier 1: Real AI Video Clip (Local Wan2.1/LTX, fal.ai, Replicate)
  - Tier 2: AI Still Image (fal/Replicate/Procedural) + Ken Burns Slow Pan/Zoom via FFmpeg
  - Tier 3: Local Pre-made Motion-Loop Background (Guaranteed Offline Fallback)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.config import FFMPEG_BIN, TEMP_DIR
from src.logger import get_logger
from src.scene_providers import generate_still_image
from src.template_backgrounds import get_template_background

log = get_logger(__name__)

FALLBACK_CACHE_DIR = TEMP_DIR / "scene_fallback_cache"


def _render_ken_burns_image(img_path: Path, duration: float, seg_idx: int) -> Optional[Path]:
    """Animate a still image with Ken Burns slow pan/zoom rendered locally via FFmpeg."""
    FALLBACK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dur = max(0.5, round(duration, 2))
    out_path = FALLBACK_CACHE_DIR / f"kenburns_{seg_idx:03d}_{dur:.2f}s.mp4"

    if out_path.exists() and out_path.stat().st_size > 5000:
        return out_path

    total_frames = int(round(dur * 30))
    # Rotate zoom in vs zoom out based on segment index
    if seg_idx % 2 == 0:
        zoom_expr = f"min(pzoom+0.0015,1.25)"
        x_expr = "(iw-x)/2"
        y_expr = "(ih-y)/2"
    else:
        zoom_expr = f"if(eq(on,1),1.25,max(1.0,pzoom-0.0015))"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"

    vf = (
        f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        f"zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}':d={total_frames}:s=1080x1920:fps=30"
    )

    cmd = [
        FFMPEG_BIN, "-y",
        "-loop", "1",
        "-i", str(img_path),
        "-vf", vf,
        "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", "30",
        "-an",
        str(out_path),
    ]
    from src.ffmpeg_utils import run_ffmpeg
    try:
        res = run_ffmpeg(cmd, timeout=90)
        if res.returncode == 0 and out_path.exists() and out_path.stat().st_size > 5000:
            log.info("Tier 2 Fallback: Rendered Ken Burns pan/zoom video for seg %d (%.1fs)", seg_idx + 1, dur)
            return out_path
        log.warning("Tier 2 Fallback: Ken Burns FFmpeg failed: %s", res.stderr[-200:])
    except Exception as e:
        log.warning("Tier 2 Fallback: Exception during Ken Burns render: %s", e)

    return None


def resolve_segment_visual(
    segment: dict,
    seg_idx: int,
    scene_video_path: Optional[Path] = None,
    visual_desc: Optional[str] = None,
) -> tuple[Path, str]:
    """Resolve visual clip for segment in order: Tier 1 -> Tier 2 -> Tier 3.

    Returns (clip_path, tier_label). Guaranteed to return a valid Path.
    """
    seg_start = segment.get("start", 0.0)
    seg_end = segment.get("end", seg_start + 5.0)
    duration = max(0.5, seg_end - seg_start)

    # ── Tier 1: Real AI Video Clip ──
    if scene_video_path and Path(scene_video_path).exists() and Path(scene_video_path).stat().st_size > 5000:
        log.info("Seg %d visual: Tier 1 (Real AI Video) -> %s", seg_idx + 1, Path(scene_video_path).name)
        return Path(scene_video_path), "Tier 1: AI Video"

    log.info("Seg %d visual: Tier 1 unavailable, attempting Tier 2 (AI Still Image + Ken Burns)...", seg_idx + 1)

    # ── Tier 2: AI Still Image + Ken Burns slow pan/zoom ──
    prompt = visual_desc or segment.get("text", "") or f"Cinematic scene for segment {seg_idx + 1}"
    try:
        img_path = generate_still_image(prompt=prompt, width=1080, height=1920)
        if img_path and Path(img_path).exists():
            kb_clip = _render_ken_burns_image(Path(img_path), duration, seg_idx)
            if kb_clip:
                log.info("Seg %d visual: Tier 2 (Ken Burns Animated AI Image) -> %s", seg_idx + 1, kb_clip.name)
                return kb_clip, "Tier 2: Animated AI Image"
    except Exception as e:
        log.warning("Seg %d Tier 2 attempt failed: %s", seg_idx + 1, e)

    # ── Tier 3: Pre-made Motion-Loop Background (Guaranteed Offline Fallback) ──
    log.info("Seg %d visual: Tier 2 unavailable, using Tier 3 (Template Motion Background)...", seg_idx + 1)
    bg_clip = get_template_background(duration=duration, seed=seg_idx)
    if bg_clip and Path(bg_clip).exists():
        log.info("Seg %d visual: Tier 3 (Template Motion Background) -> %s", seg_idx + 1, Path(bg_clip).name)
        return Path(bg_clip), "Tier 3: Template Background"

    # Ultimate last resort: solid-color procedural background — guaranteed to work
    # with zero external calls / zero GPU / zero template assets.
    log.warning("Seg %d: Tier 3 unavailable, falling back to solid-color background", seg_idx + 1)
    return _generate_solid_bg(duration, seed=seg_idx), "Tier 3: Solid Fallback"


def _generate_solid_bg(duration: float, seed: int = 0) -> Path:
    """Generate a solid-color 9:16 mp4 via FFmpeg — never fails, no network/GPU."""
    FALLBACK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dur = max(0.5, round(duration, 2))
    out = FALLBACK_CACHE_DIR / f"solid_{seed}_{dur:.2f}s.mp4"
    if out.exists() and out.stat().st_size > 5000:
        return out
    # Deterministic dark gradient color per seed
    import random as _rng
    rng = _rng.Random(seed)
    r, g, b = rng.randint(10, 40), rng.randint(10, 30), rng.randint(25, 60)
    color = f"0x{r:02x}{g:02x}{b:02x}"
    cmd = [
        FFMPEG_BIN, "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s=1080x1920:r=30:d={dur:.3f}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", "30",
        "-an", str(out),
    ]
    from src.ffmpeg_utils import run_ffmpeg
    try:
        res = run_ffmpeg(cmd, timeout=30)
        if res.returncode == 0 and out.exists() and out.stat().st_size > 1000:
            return out
    except Exception as e:
        log.warning("Solid bg fallback failed: %s", e)
    # If even FFmpeg fails, return the lightest possible cached path
    return out
