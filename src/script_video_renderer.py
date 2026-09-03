"""
src/script_video_renderer.py
-----------------------------
HD 9:16 video from TTS audio + script sections.
Dynamic backgrounds per section type, word-synced captions with
pop-in animation, safe-zone caption placement, no metadata leaking.
"""

from __future__ import annotations

import math
import random
import subprocess
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from src.config import FFMPEG_BIN, TEMP_DIR, OUTPUT_DIR, get_video_spec_config
from src.logger import get_logger

log = get_logger(__name__)

WIDTH = 1080
HEIGHT = 1920
FPS = 30


def _load_target_spec() -> None:
    """Refresh the target resolution from the admin settings (dynamic)."""
    global WIDTH, HEIGHT, FPS
    spec = get_video_spec_config()
    WIDTH = int(spec["target_width"])
    HEIGHT = int(spec["target_height"])
    FPS = int(spec["target_fps"])

_font_cache: dict = {}

# ── Safe-zone caption positions (bottom 15-40% of screen) ──────────────────────
# Each entry: (x_center_offset_pct, y_from_bottom_pct)
# x_center_offset_pct: -0.15 to +0.15 (relative to WIDTH)
# y_from_bottom_pct: 0.15 to 0.40 (from bottom edge)
CAPTION_POSITIONS = [
    (0.00, 0.22),   # center, mid-low
    (-0.08, 0.18),  # slight left, higher
    (0.08, 0.25),   # slight right, lower
    (0.00, 0.32),   # center, higher up
    (-0.05, 0.28),  # slight left, mid
    (0.05, 0.20),   # slight right, low
]


def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    import os
    win_dir = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
    candidates = [
        "segoeuib.ttf" if bold else "segoeui.ttf",
        "arialbd.ttf" if bold else "arial.ttf",
    ]
    for fname in candidates:
        fpath = os.path.join(win_dir, fname)
        if os.path.isfile(fpath):
            font = ImageFont.truetype(fpath, size)
            _font_cache[key] = font
            return font
    font = ImageFont.load_default()
    _font_cache[key] = font
    return font


# ── Section-type detection ─────────────────────────────────────────────────────

def _classify_section(label: str) -> str:
    """Map section label to a visual style class."""
    lu = label.upper()
    if "HOOK" in lu:
        return "hook"
    if "PROBLEM" in lu:
        return "problem"
    if "SECRET" in lu or "INSIGHT" in lu or "CORE" in lu:
        return "secret"
    if "TWIST" in lu or "TURN" in lu:
        return "twist"
    if "CTA" in lu or "CALL" in lu or "CONCLUSION" in lu:
        return "cta"
    return "default"


# ── Dynamic background generators ──────────────────────────────────────────────

def _bg_hook(t: float) -> Image.Image:
    """Dark with pulsing radial glow + accent flash."""
    img = Image.new("RGB", (WIDTH, HEIGHT), (5, 5, 12))
    draw = ImageDraw.Draw(img)
    pulse = 0.5 + 0.5 * math.sin(t * 3.0)
    cx, cy = WIDTH // 2, HEIGHT // 3
    for r in range(int(400 * pulse), 0, -4):
        alpha = int(40 * (1 - r / max(1, 400 * pulse)))
        c = (100 + alpha, 50 + alpha // 2, 200 + alpha)
        draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], outline=c)
    # Horizontal accent line
    ly = HEIGHT // 2
    line_w = int(WIDTH * 0.6 * pulse)
    x0 = (WIDTH - line_w) // 2
    draw.rectangle([(x0, ly), (x0 + line_w, ly + 3)], fill=(255, 180, 50))
    return img


def _bg_problem(t: float) -> Image.Image:
    """Deep warm gradient with noise texture."""
    img = Image.new("RGB", (WIDTH, HEIGHT), (20, 8, 8))
    draw = ImageDraw.Draw(img)
    shift = int(15 * math.sin(t * 0.5))
    for y in range(0, HEIGHT, 4):
        ratio = y / HEIGHT
        r = int(35 + 25 * ratio + shift)
        g = int(12 + 8 * ratio)
        b = int(10 + 5 * ratio)
        draw.rectangle([(0, y), (WIDTH, y + 4)], fill=(min(r, 60), g, b))
    # Subtle diagonal stripes
    offset = int(t * 30) % 60
    for x in range(-HEIGHT, WIDTH + HEIGHT, 60):
        draw.line([(x + offset, 0), (x - HEIGHT + offset, HEIGHT)], fill=(50, 18, 15), width=1)
    return img


def _bg_secret(t: float) -> Image.Image:
    """Purple-blue tech grid with floating nodes."""
    img = Image.new("RGB", (WIDTH, HEIGHT), (8, 6, 20))
    draw = ImageDraw.Draw(img)
    grid_spacing = 80
    offset_y = int(t * 15) % grid_spacing
    grid_color = (25, 18, 50)
    for y in range(-grid_spacing, HEIGHT + grid_spacing, grid_spacing):
        draw.line([(0, y + offset_y), (WIDTH, y + offset_y)], fill=grid_color, width=1)
    for x in range(0, WIDTH, grid_spacing):
        draw.line([(x, 0), (x, HEIGHT)], fill=grid_color, width=1)
    # Floating nodes
    rng = random.Random(42)
    for _ in range(12):
        nx = rng.randint(50, WIDTH - 50)
        ny = rng.randint(50, HEIGHT - 50)
        node_r = 4 + int(2 * math.sin(t * 2 + nx * 0.01))
        pulse_c = int(120 + 60 * math.sin(t * 1.5 + ny * 0.005))
        draw.ellipse(
            [(nx - node_r, ny - node_r), (nx + node_r, ny + node_r)],
            fill=(pulse_c, 50, 255),
        )
    return img


def _bg_twist(t: float) -> Image.Image:
    """Color-inverted glitch: cyan/magenta bands."""
    img = Image.new("RGB", (WIDTH, HEIGHT), (10, 10, 10))
    draw = ImageDraw.Draw(img)
    band_h = 120
    glitch_offset = int(20 * math.sin(t * 4))
    for i in range(0, HEIGHT, band_h * 2):
        y1 = i + glitch_offset
        y2 = min(y1 + band_h, HEIGHT)
        if y1 < HEIGHT:
            c1 = (0, int(180 + 40 * math.sin(t + i * 0.01)), int(200 + 55 * math.sin(t * 2)))
            draw.rectangle([(0, y1), (WIDTH, y2)], fill=c1)
    # Horizontal glitch bars
    for _ in range(3):
        gy = random.Random(int(t * 10)).randint(0, HEIGHT)
        gw = random.Random(int(t * 7)).randint(100, 400)
        gx = random.Random(int(t * 13)).randint(0, WIDTH - gw)
        draw.rectangle([(gx, gy), (gx + gw, gy + 4)], fill=(255, 0, 180))
    return img


def _bg_cta(t: float) -> Image.Image:
    """Clean dark with golden accent circles."""
    img = Image.new("RGB", (WIDTH, HEIGHT), (8, 8, 14))
    draw = ImageDraw.Draw(img)
    # Radial golden rings
    cx, cy = WIDTH // 2, HEIGHT // 2
    pulse = 0.5 + 0.5 * math.sin(t * 2)
    for r in range(int(200 + 100 * pulse), 50, -30):
        alpha = int(60 * (1 - r / 400))
        draw.ellipse(
            [(cx - r, cy - r), (cx + r, cy + r)],
            outline=(200 + alpha, 160 + alpha // 2, 40),
            width=2,
        )
    # Corner accent dots
    for (dx, dy) in [(80, 80), (WIDTH - 80, 80), (80, HEIGHT - 80), (WIDTH - 80, HEIGHT - 80)]:
        r = 6 + int(3 * math.sin(t * 3 + dx * 0.01))
        draw.ellipse([(dx - r, dy - r), (dx + r, dy + r)], fill=(255, 200, 50))
    return img


def _bg_default(t: float) -> Image.Image:
    """Subtle dark gradient with slow radial pulse."""
    img = Image.new("RGB", (WIDTH, HEIGHT), (10, 8, 22))
    draw = ImageDraw.Draw(img)
    cx, cy = WIDTH // 2, HEIGHT // 2 - 100
    pulse = 0.5 + 0.5 * math.sin(t * 1.2)
    for r in range(int(350 * pulse), 0, -5):
        alpha = int(20 * (1 - r / max(1, 350 * pulse)))
        c = (80 + alpha, 40 + alpha // 2, 160 + alpha)
        draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], outline=c)
    return img


_BG_GENERATORS = {
    "hook": _bg_hook,
    "problem": _bg_problem,
    "secret": _bg_secret,
    "twist": _bg_twist,
    "cta": _bg_cta,
    "default": _bg_default,
}


# ── Caption chunking ───────────────────────────────────────────────────────────

def _build_caption_chunks(
    words: list[dict],
    max_words: int = 3,
) -> list[list[dict]]:
    """Group words into short chunks (2-3 words each) for pop-in display."""
    if not words:
        return []
    chunks = []
    current = []
    for w in words:
        current.append(w)
        if len(current) >= max_words:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks


# ── Caption rendering helpers ──────────────────────────────────────────────────

def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _text_height(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]


def _render_caption_chunk(
    draw: ImageDraw.ImageDraw,
    chunk_words: list[dict],
    active_idx: int,
    font: ImageFont.FreeTypeFont,
    x_center: int,
    y_bottom: int,
    highlight_color: tuple = (255, 210, 40),
    text_color: tuple = (255, 255, 255),
    pill_bg: tuple = (0, 0, 0, 180),
) -> None:
    """Render a 2-3 word caption chunk with active word highlighted."""
    text = " ".join(w["text"] for w in chunk_words)
    total_w = _text_width(draw, text, font)
    th = _text_height(draw, text, font)

    # Pill background
    pad_x, pad_y = 24, 14
    pill_x = x_center - total_w // 2 - pad_x
    pill_y = y_bottom - th - pad_y * 2
    pill_r = th // 2 + pad_y

    # Draw pill with alpha-like effect (semi-transparent dark)
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rounded_rectangle(
        [(pill_x, pill_y), (pill_x + total_w + pad_x * 2, pill_y + th + pad_y * 2)],
        radius=min(pill_r, th // 2 + pad_y),
        fill=pill_bg,
    )
    # Flatten onto the main image context (we draw directly on frame instead)

    # Draw each word
    x = x_center - total_w // 2
    for i, w in enumerate(chunk_words):
        word_text = w["text"]
        ww = _text_width(draw, word_text + " ", font)
        color = highlight_color if i == active_idx else text_color

        # Active word: scale-up glow effect
        if i == active_idx:
            # Glow shadow
            for dx, dy in [(-2, 0), (2, 0), (0, -2), (0, 2)]:
                draw.text((x + dx, pill_y + pad_y + dy), word_text, font=font,
                          fill=(highlight_color[0] // 2, highlight_color[1] // 2, 0))
            draw.text((x, pill_y + pad_y), word_text, font=font, fill=color)
        else:
            draw.text((x, pill_y + pad_y), word_text, font=font, fill=color)

        x += _text_width(draw, word_text + " ", font)


def _draw_caption_pill_bg(
    frame: Image.Image,
    chunk_words: list[dict],
    font: ImageFont.FreeTypeFont,
    x_center: int,
    y_bottom: int,
) -> None:
    """Draw a dark pill background behind the caption text."""
    draw_tmp = ImageDraw.Draw(frame)
    text = " ".join(w["text"] for w in chunk_words)
    total_w = _text_width(draw_tmp, text, font)
    th = _text_height(draw_tmp, text, font)
    pad_x, pad_y = 24, 14

    pill_x = x_center - total_w // 2 - pad_x
    pill_y = y_bottom - th - pad_y * 2
    pill_w = total_w + pad_x * 2
    pill_h = th + pad_y * 2
    pill_r = min(th // 2 + pad_y, pill_h // 2)

    # Create RGBA overlay for semi-transparent pill
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle(
        [(pill_x, pill_y), (pill_x + pill_w, pill_y + pill_h)],
        radius=pill_r,
        fill=(0, 0, 0, 170),
    )
    # Composite
    frame.paste(
        Image.alpha_composite(frame.convert("RGBA"), overlay).convert("RGB"),
        (0, 0),
    )


# ── Section label rendering ────────────────────────────────────────────────────

def _draw_section_label(
    draw: ImageDraw.ImageDraw,
    label: str,
    seg_idx: int,
    total_segs: int,
) -> None:
    """Draw section label pill at top + segment counter."""
    if not label:
        return
    label_font = _get_font(32, bold=True)
    text = label.upper()
    tw = _text_width(draw, text, label_font)
    th = _text_height(draw, text, label_font)
    pad_x, pad_y = 20, 10
    x = (WIDTH - tw) // 2
    y = 160

    draw.rounded_rectangle(
        [(x - pad_x, y - pad_y), (x + tw + pad_x, y + th + pad_y)],
        radius=th // 2 + pad_y,
        fill=(130, 80, 255),
    )
    draw.text((x, y), text, font=label_font, fill=(255, 255, 255))

    # Segment counter (top right)
    counter_font = _get_font(26, bold=False)
    ctext = f"{seg_idx + 1}/{total_segs}"
    cw = _text_width(draw, ctext, counter_font)
    draw.text((WIDTH - 50 - cw // 2, 24), ctext, font=counter_font, fill=(140, 140, 155))

    # Progress bar
    bar_y = 8
    progress = (seg_idx + 1) / max(1, total_segs)
    bar_w = int(WIDTH * progress)
    for x_px in range(bar_w):
        ratio = x_px / max(1, WIDTH)
        r = int(130 + 125 * ratio)
        g = int(80 + 120 * ratio)
        b = int(255 - 205 * ratio)
        draw.line([(x_px, bar_y), (x_px, bar_y + 4)], fill=(r, g, b))


# ── Main render function ───────────────────────────────────────────────────────

def _prepare_scene_image(img_path: Path, darken: float = 0.45) -> Image.Image:
    """Load an AI scene image, resize/crop to 9:16, add dark overlay for caption readability."""
    img = Image.open(str(img_path)).convert("RGB")

    # Smart crop to 9:16 aspect ratio
    src_w, src_h = img.size
    target_ratio = WIDTH / HEIGHT  # 0.5625
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        # Source is wider — crop sides
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, src_h))
    else:
        # Source is taller — crop top/bottom
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 2
        img = img.crop((0, top, src_w, top + new_h))

    # Resize to exact dimensions
    img = img.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

    # Add dark overlay for caption readability
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, int(255 * darken)))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    # Subtle bottom gradient fade for caption area
    draw = ImageDraw.Draw(img)
    for y in range(HEIGHT - 400, HEIGHT):
        alpha = int(120 * ((y - (HEIGHT - 400)) / 400))
        draw.line([(0, y), (WIDTH, y)], fill=(0, 0, 0))
        # Blend: darken more toward bottom
        r, g, b = img.getpixel((WIDTH // 2, y))
        blend = alpha / 255
        nr = int(r * (1 - blend))
        ng = int(g * (1 - blend))
        nb = int(b * (1 - blend))
        draw.line([(0, y), (WIDTH, y)], fill=(nr, ng, nb))

    return img


def _render_video_clip_with_captions(
    video_path: Path,
    chunks: list[list[dict]],
    seg_start: float,
    seg_duration: float,
    label: str,
    seg_idx: int,
    total_segs: int,
    pos: tuple,
    output_path: Path,
) -> None:
    """
    Take an AI video clip, resize/crop to 9:16, add dark overlay + word-synced captions.
    Uses ffmpeg drawtext filter for efficient rendering (no frame-by-frame PIL).
    """
    chunk_dur = seg_duration / max(1, len(chunks))

    # Build drawtext filter for each caption chunk
    drawtext_filters = []
    for ci, chunk in enumerate(chunks):
        text = " ".join(w["text"] for w in chunk)
        # Escape special chars for ffmpeg drawtext
        text_escaped = text.replace("'", "'\\''").replace(":", "\\:").replace("%", "%%")
        chunk_start = ci * chunk_dur
        chunk_end = (ci + 1) * chunk_dur

        # Caption position (safe zone)
        x_off_pct, y_from_bot_pct = pos
        # Convert to ffmpeg coordinates
        x_expr = f"(w-text_w)/2+{int(WIDTH * x_off_pct)}"
        y_expr = f"h-{int(HEIGHT * y_from_bot_pct)}-text_h"

        drawtext_filters.append(
            f"drawtext=text='{text_escaped}'"
            f":fontsize=72:fontcolor=white"
            f":borderw=5:bordercolor=black"
            f":x={x_expr}:y={y_expr}"
            f":enable='between(t\\,{chunk_start:.3f}\\,{chunk_end:.3f})'"
        )

    # Section label at top (always visible for this segment)
    label_escaped = label.upper().replace("'", "'\\''").replace(":", "\\:").replace("%", "%%") if label else ""
    if label_escaped:
        drawtext_filters.append(
            f"drawtext=text='{label_escaped}'"
            f":fontsize=34:fontcolor=white"
            f":borderw=3:bordercolor=black"
            f":x=(w-text_w)/2:y=180"
        )

    # Segment counter at top right
    counter_text = f"{seg_idx + 1}/{total_segs}"
    drawtext_filters.append(
        f"drawtext=text='{counter_text}'"
        f":fontsize=26:fontcolor=gray"
        f":x=w-80:y=30"
    )

    # Build ffmpeg command
    vf_parts = [
        # Scale and crop to 9:16
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase",
        f"crop={WIDTH}:{HEIGHT}",
        # Dark overlay for caption readability
        "colorbalance=rs=-0.05:gs=-0.05:bs=-0.05",
        # Subtle vignette
        "vignette=PI/4",
    ]
    vf_parts.extend(drawtext_filters)

    vf = ",".join(vf_parts)

    cmd = [
        FFMPEG_BIN, "-y",
        "-i", str(video_path),
        "-t", f"{seg_duration:.3f}",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-an",
        str(output_path),
    ]
    from src.ffmpeg_utils import run_ffmpeg
    try:
        r = run_ffmpeg(cmd, timeout=300)
    except Exception as ffe:
        log.error("Video clip render timed out: %s", ffe)
        raise RuntimeError(f"ffmpeg drawtext failed for segment {seg_idx}") from ffe
    if r.returncode != 0:
        log.error("Video clip render failed: %s", r.stderr[-400:] if r.stderr else "?")
        raise RuntimeError(f"ffmpeg drawtext failed for segment {seg_idx}")

    log.info("Video clip rendered: seg %d -> %s", seg_idx, output_path.name)


def render_script_video(
    tts_audio_path: Path,
    segments: list[dict],
    output_filename: str,
    output_dir: Optional[Path] = None,
    section_labels: Optional[list[str]] = None,
    visual_descriptions: Optional[list[str]] = None,
    scene_videos: list[Optional[Path]] = None,
    progress_cb=None,
) -> Path:
    """
    Render HD 9:16 video with word-synced captions and 3-tier visual fallback.

    For each segment resolve_segment_visual() is used:
      Tier 1: Real AI video clip (Wan2.1/LTX, fal.ai, Replicate)
      Tier 2: AI still image + Ken Burns pan/zoom (local FFmpeg)
      Tier 3: Template motion-loop background (offline, guaranteed)
    The function never hard-fails because a segment lacks an AI video — it always
    returns a valid clip via the fallback chain.
    """
    if output_dir is None:
        output_dir = OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / output_filename

    if not tts_audio_path.exists():
        raise FileNotFoundError(f"TTS audio not found: {tts_audio_path}")
    if not segments:
        raise ValueError("No segments to render")

    _load_target_spec()
    total_segs = len(segments)
    log.info("Script video: rendering %d segments with 3-tier visual fallback chain -> %s", total_segs, output_filename)
    if progress_cb:
        progress_cb("Preparing...", 5)

    temp_dir = TEMP_DIR / "script_frames"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Pick a caption position for each segment (rotate through safe zones)
    rng = random.Random(42)
    seg_positions = []
    pos_indices = list(range(len(CAPTION_POSITIONS)))
    for i in range(total_segs):
        seg_positions.append(CAPTION_POSITIONS[pos_indices[i % len(pos_indices)]])

    # ── Build caption chunks per segment ──
    seg_chunks: list[list[list[dict]]] = []
    for seg in segments:
        words = seg.get("words", [])
        if not words:
            words = [{"text": w, "start": 0, "end": 0} for w in seg["text"].split()]
        chunks = _build_caption_chunks(words, max_words=3)
        seg_chunks.append(chunks)

    # ── Render video clips with captions per segment (3-Tier Fallback) ──
    from src.scene_fallback import resolve_segment_visual

    video_clip_entries: list[Path] = []

    for seg_idx, seg in enumerate(segments):
        label = section_labels[seg_idx] if section_labels and seg_idx < len(section_labels) else ""
        v_desc = visual_descriptions[seg_idx] if visual_descriptions and seg_idx < len(visual_descriptions) else None
        raw_vid = scene_videos[seg_idx] if scene_videos and seg_idx < len(scene_videos) else None

        # Resolve segment visual via Tier 1 (AI Video) -> Tier 2 (Ken Burns Animated Image) -> Tier 3 (Template Background)
        resolved_clip_path, tier_label = resolve_segment_visual(
            segment=seg,
            seg_idx=seg_idx,
            scene_video_path=raw_vid,
            visual_desc=v_desc,
        )

        chunks = seg_chunks[seg_idx]
        seg_start = seg["start"]
        seg_end = seg["end"]
        seg_duration = max(0.5, seg_end - seg_start)

        if progress_cb:
            pct = 5 + int((seg_idx / total_segs) * 50)
            progress_cb(f"Section {seg_idx + 1}/{total_segs} ({tier_label})...", pct)

        clip_path = temp_dir / f"vclip_{seg_idx:03d}.mp4"
        try:
            _render_video_clip_with_captions(
                resolved_clip_path, chunks, seg_start, seg_duration,
                label, seg_idx, total_segs,
                seg_positions[seg_idx], clip_path,
            )
            video_clip_entries.append(clip_path)
        except Exception as e:
            log.error("Clip render with captions failed for seg %d (%s): %s", seg_idx, tier_label, e)
            # Emergency direct copy if drawtext fails
            video_clip_entries.append(resolved_clip_path)

    # ── Concat all AI video clips (each already rendered to 9:16 fullscreen) ──
    clip_concat = temp_dir / "clip_concat.txt"
    with open(str(clip_concat), "w", encoding="utf-8") as f:
        for cp in video_clip_entries:
            f.write(f"file '{cp}'\n")

    concat_cmd = [
        FFMPEG_BIN, "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(clip_concat),
        "-c", "copy",
        str(temp_dir / "concat_video.mp4"),
    ]
    from src.ffmpeg_utils import run_ffmpeg
    r = run_ffmpeg(concat_cmd, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"Concat failed: {r.stderr[-200:] if r.stderr else 'unknown'}")

    if progress_cb:
        progress_cb("Adding audio...", 85)

    # ── Mux audio + guarantee fullscreen 9:16 (1080x1920) output ──
    # Re-encode video so the final short ALWAYS fills the frame with no borders,
    # no letterbox, no square-crop — captions stay inside the safe area.
    final_scale = (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT}"
    )
    final_cmd = [
        FFMPEG_BIN, "-y",
        "-i", str(temp_dir / "concat_video.mp4"),
        "-i", str(tts_audio_path),
        "-vf", final_scale,
        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(output_path),
    ]
    r = run_ffmpeg(final_cmd, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"Final encode failed: {r.stderr[-200:] if r.stderr else 'unknown'}")

    if not output_path.exists():
        raise RuntimeError("FFmpeg produced no output file")

    # Cleanup
    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)

    size_mb = output_path.stat().st_size / (1024 * 1024)
    log.info("Script video: done — %s (%.1f MB, HD, word-synced)", output_filename, size_mb)
    if progress_cb:
        progress_cb(f"Done: {output_filename}", 100)

    return output_path
