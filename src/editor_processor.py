"""
src/editor_processor.py
-----------------------
Processes post-generation video editing requests:
- Video trimming (start_time, end_time)
- Visual filters & color grading (presets + brightness, contrast, saturation)
- Audio pitch shifting (in semitones, e.g. -6 to +6)
- Audio tempo/speed adjustment
- Audio volume adjustment
- Re-encoding with high quality H.264 + AAC
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from src.logger import get_logger
from src.config import FFMPEG_BIN

log = get_logger("editor_processor")


# Preset visual filter definitions for FFmpeg
# Maps preset name -> FFmpeg video filter string
FILTER_PRESETS: dict[str, str] = {
    "none": "",
    "vivid": "eq=saturation=1.5:contrast=1.15",
    "warm": "colorbalance=rs=0.15:gs=0.05:bs=-0.15:rm=0.1:gm=0.03:bm=-0.1,eq=saturation=1.2",
    "cool": "colorbalance=rs=-0.1:gs=0.02:bs=0.15:rm=-0.08:gm=0.0:bm=0.12,eq=saturation=1.1",
    "bw": "hue=s=0,eq=contrast=1.2:brightness=0.02",
    "cinema": "colorbalance=rs=0.08:gs=-0.02:bs=-0.05,eq=contrast=1.18:saturation=1.25",
    "neon": "hue=h=30:s=1.8,eq=contrast=1.15:brightness=-0.05",
    "vintage": "colorbalance=rs=0.2:gs=0.1:bs=-0.1,eq=saturation=0.85:contrast=0.95:brightness=0.05",
}


def build_video_filter(
    preset: str = "none",
    brightness: float = 100.0,  # 0 to 200 (100 is normal)
    contrast: float = 100.0,    # 0 to 200 (100 is normal)
    saturation: float = 100.0,  # 0 to 200 (100 is normal)
    sharpen: float = 0.0,       # 0 to 100
) -> Optional[str]:
    """
    Constructs an FFmpeg video filter graph from visual adjustments and presets.
    """
    filters = []

    # Apply base adjustment (eq filter)
    # FFmpeg eq: brightness is -1.0 to 1.0 (default 0.0), contrast is -1000 to 1000 (default 1.0), saturation is 0.0 to 3.0 (default 1.0)
    b_val = (brightness - 100.0) / 100.0 * 0.5  # maps 0..200 to -0.5..0.5
    c_val = contrast / 100.0
    s_val = saturation / 100.0

    eq_parts = []
    if abs(b_val) > 0.001:
        eq_parts.append(f"brightness={b_val:.3f}")
    if abs(c_val - 1.0) > 0.001:
        eq_parts.append(f"contrast={c_val:.3f}")
    if abs(s_val - 1.0) > 0.001:
        eq_parts.append(f"saturation={s_val:.3f}")

    if eq_parts:
        filters.append(f"eq={':'.join(eq_parts)}")

    # Apply Preset
    preset_str = FILTER_PRESETS.get(preset.lower().strip(), "")
    if preset_str:
        filters.append(preset_str)

    # Apply Sharpening if requested
    if sharpen > 0:
        # unsharp=lx:ly:la:cx:cy:ca (la is luma strength, default 1.0)
        luma_amount = (sharpen / 100.0) * 1.5
        filters.append(f"unsharp=5:5:{luma_amount:.2f}:3:3:0.0")

    return ",".join(filters) if filters else None


def build_audio_filter(
    pitch_semitones: float = 0.0,  # -12 to +12 semitones
    speed: float = 1.0,            # 0.5 to 2.0x playback rate
    volume: float = 100.0,         # 0 to 200%
    sample_rate: int = 48000,
) -> Optional[str]:
    """
    Constructs an FFmpeg audio filter chain for pitch shifting, speed, and volume.
    
    Pitch shift formula without changing speed:
    1. asetrate = sample_rate * 2^(semitones / 12) -> alters pitch and speed
    2. aresample = sample_rate                     -> restores correct sample rate
    3. atempo = 1 / (2^(semitones / 12))           -> compensates speed back to 1.0
    4. atempo = speed                              -> applies desired user speed
    5. volume = volume / 100.0
    """
    filters = []

    # Volume
    if abs(volume - 100.0) > 0.01:
        vol_factor = max(0.0, volume / 100.0)
        filters.append(f"volume={vol_factor:.2f}")

    # Pitch & Speed
    has_pitch = abs(pitch_semitones) > 0.01
    has_speed = abs(speed - 1.0) > 0.01

    if has_pitch:
        pitch_factor = 2.0 ** (pitch_semitones / 12.0)
        new_rate = int(sample_rate * pitch_factor)
        compensating_tempo = 1.0 / pitch_factor

        filters.append(f"asetrate={new_rate}")
        filters.append(f"aresample={sample_rate}")

        # Chain atempo if needed (atempo accepts 0.5 to 2.0 in one step)
        total_tempo = compensating_tempo * speed
        tempo_filters = _split_atempo(total_tempo)
        filters.extend(tempo_filters)
    elif has_speed:
        tempo_filters = _split_atempo(speed)
        filters.extend(tempo_filters)

    return ",".join(filters) if filters else None


def _split_atempo(tempo: float) -> list[str]:
    """Splits atempo if outside FFmpeg's 0.5..2.0 range."""
    res = []
    current = max(0.1, min(10.0, tempo))
    while current > 2.0:
        res.append("atempo=2.0")
        current /= 2.0
    while current < 0.5:
        res.append("atempo=0.5")
        current /= 0.5
    if abs(current - 1.0) > 0.005:
        res.append(f"atempo={current:.3f}")
    return res


def export_edited_video(
    input_path: Path,
    output_path: Path,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    preset: str = "none",
    brightness: float = 100.0,
    contrast: float = 100.0,
    saturation: float = 100.0,
    sharpen: float = 0.0,
    pitch_semitones: float = 0.0,
    speed: float = 1.0,
    volume: float = 100.0,
) -> Path:
    """
    Applies trimming, visual filters, and audio pitch/speed processing to a video.
    
    Returns
    -------
    Path
        Path to the exported video file.
    """
    if not input_path.exists():
        raise FileNotFoundError(f"Input video not found: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [FFMPEG_BIN, "-y"]

    # Fast seek before input if start_time is given
    if start_time is not None and start_time > 0:
        cmd.extend(["-ss", f"{start_time:.3f}"])

    cmd.extend(["-i", str(input_path.resolve())])

    # End duration
    if end_time is not None and end_time > 0:
        if start_time is not None and start_time > 0:
            duration = max(0.1, end_time - start_time)
            cmd.extend(["-t", f"{duration:.3f}"])
        else:
            cmd.extend(["-to", f"{end_time:.3f}"])

    # Video filters
    vf = build_video_filter(
        preset=preset,
        brightness=brightness,
        contrast=contrast,
        saturation=saturation,
        sharpen=sharpen,
    )
    if vf:
        cmd.extend(["-vf", vf])

    # Audio filters
    af = build_audio_filter(
        pitch_semitones=pitch_semitones,
        speed=speed,
        volume=volume,
    )
    if af:
        cmd.extend(["-af", af])

    # Encoding settings
    cmd.extend([
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path.resolve()),
    ])

    log.info("Running FFmpeg export: %s", " ".join(cmd))
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if proc.returncode != 0:
        log.error("FFmpeg export failed with code %d: %s", proc.returncode, proc.stderr)
        raise RuntimeError(f"FFmpeg export failed: {proc.stderr[-500:]}")

    log.info("Export successfully created: %s", output_path)
    return output_path
