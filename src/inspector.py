"""
inspector.py
------------
Phase 1 module: Extract and display technical metadata from a video file
using FFprobe (part of the FFmpeg suite).

Public API:
    inspect_video(path: Path) -> VideoInfo
        Returns a VideoInfo dataclass with streams & container info.

    print_video_info(info: VideoInfo) -> None
        Pretty-prints the VideoInfo to the console.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.config import FFPROBE_BIN
from src.logger import get_logger

log = get_logger(__name__)


# ── Data Classes ───────────────────────────────────────────────────────────────

@dataclass
class StreamInfo:
    """Metadata for a single codec stream (video or audio)."""
    index: int
    codec_type: str          # "video" | "audio" | "subtitle" | "data"
    codec_name: str
    codec_long_name: str
    # Video-specific (None for audio streams)
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    pixel_format: Optional[str] = None
    bit_rate_kbps: Optional[float] = None
    # Audio-specific (None for video streams)
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    channel_layout: Optional[str] = None
    # Common
    duration_secs: Optional[float] = None


@dataclass
class VideoInfo:
    """Aggregated metadata for a video file."""
    file_path: Path
    file_size_mb: float
    format_name: str
    format_long_name: str
    duration_secs: float
    bit_rate_kbps: float
    streams: list[StreamInfo] = field(default_factory=list)

    # ── Convenience properties ─────────────────────────────────────────────
    @property
    def duration_human(self) -> str:
        total = int(self.duration_secs)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    @property
    def video_stream(self) -> Optional[StreamInfo]:
        return next((s for s in self.streams if s.codec_type == "video"), None)

    @property
    def audio_stream(self) -> Optional[StreamInfo]:
        return next((s for s in self.streams if s.codec_type == "audio"), None)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _require_ffprobe() -> None:
    """Raise a clear RuntimeError if ffprobe is not found on PATH."""
    if shutil.which(FFPROBE_BIN) is None:
        raise RuntimeError(
            f"'{FFPROBE_BIN}' was not found on your PATH.\n"
            "Please install FFmpeg and make sure it is accessible:\n"
            "  https://ffmpeg.org/download.html\n"
            "  (On Windows, add the bin/ folder to your PATH environment variable.)"
        )


def _safe_float(value: object, default: float = 0.0) -> float:
    """Convert *value* to float, returning *default* on failure."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _parse_fps(avg_frame_rate: str) -> Optional[float]:
    """Parse FFprobe's 'avg_frame_rate' fraction string, e.g. '30000/1001'."""
    try:
        num, den = avg_frame_rate.split("/")
        den_i = int(den)
        return round(int(num) / den_i, 3) if den_i else None
    except Exception:
        return None


def _parse_streams(raw_streams: list[dict]) -> list[StreamInfo]:
    parsed: list[StreamInfo] = []
    for s in raw_streams:
        codec_type = s.get("codec_type", "unknown")
        stream = StreamInfo(
            index=int(s.get("index", 0)),
            codec_type=codec_type,
            codec_name=s.get("codec_name", "unknown"),
            codec_long_name=s.get("codec_long_name", ""),
            duration_secs=_safe_float(s.get("duration")) or None,
        )

        if codec_type == "video":
            stream.width = int(s.get("width", 0)) or None
            stream.height = int(s.get("height", 0)) or None
            stream.fps = _parse_fps(s.get("avg_frame_rate", "0/1"))
            stream.pixel_format = s.get("pix_fmt")
            br = _safe_float(s.get("bit_rate"))
            stream.bit_rate_kbps = round(br / 1000, 1) if br else None

        elif codec_type == "audio":
            sr = _safe_float(s.get("sample_rate"))
            stream.sample_rate = int(sr) if sr else None
            stream.channels = int(s.get("channels", 0)) or None
            stream.channel_layout = s.get("channel_layout")
            br = _safe_float(s.get("bit_rate"))
            stream.bit_rate_kbps = round(br / 1000, 1) if br else None

        parsed.append(stream)
    return parsed


# ── Public API ─────────────────────────────────────────────────────────────────

def inspect_video(path: Path) -> VideoInfo:
    """
    Run FFprobe on *path* and return a :class:`VideoInfo` dataclass.

    Raises
    ------
    FileNotFoundError: If *path* does not exist.
    RuntimeError:      If ffprobe is missing or returns an error.
    """
    _require_ffprobe()

    if not path.exists():
        raise FileNotFoundError(f"Video file not found: {path}")

    log.info("Inspecting: %s", path.name)

    cmd = [
        FFPROBE_BIN,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"ffprobe exited with code {exc.returncode}.\n"
            f"stderr: {exc.stderr.strip()}"
        ) from exc

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse ffprobe JSON output: {exc}") from exc

    fmt = data.get("format", {})
    streams = _parse_streams(data.get("streams", []))

    duration = _safe_float(fmt.get("duration"))
    bit_rate = _safe_float(fmt.get("bit_rate"))
    file_size = path.stat().st_size

    return VideoInfo(
        file_path=path,
        file_size_mb=round(file_size / 1_048_576, 2),
        format_name=fmt.get("format_name", "unknown"),
        format_long_name=fmt.get("format_long_name", ""),
        duration_secs=duration,
        bit_rate_kbps=round(bit_rate / 1000, 1) if bit_rate else 0.0,
        streams=streams,
    )


def print_video_info(info: VideoInfo) -> None:
    """Pretty-print a :class:`VideoInfo` to the console."""
    sep = "─" * 60

    print(f"\n{sep}")
    print(f"  📁  {info.file_path.name}")
    print(sep)
    print(f"  Container  : {info.format_long_name}")
    print(f"  Duration   : {info.duration_human}")
    print(f"  File size  : {info.file_size_mb} MB")
    print(f"  Bit-rate   : {info.bit_rate_kbps} kbps")

    vs = info.video_stream
    if vs:
        print(f"\n  ── Video stream (index {vs.index}) ──")
        print(f"     Codec      : {vs.codec_long_name}")
        print(f"     Resolution : {vs.width}×{vs.height}")
        print(f"     Frame-rate : {vs.fps} fps")
        print(f"     Pixel fmt  : {vs.pixel_format}")
        if vs.bit_rate_kbps:
            print(f"     Bit-rate   : {vs.bit_rate_kbps} kbps")

    as_ = info.audio_stream
    if as_:
        print(f"\n  ── Audio stream (index {as_.index}) ──")
        print(f"     Codec      : {as_.codec_long_name}")
        print(f"     Sample rate: {as_.sample_rate} Hz")
        print(f"     Channels   : {as_.channels} ({as_.channel_layout})")
        if as_.bit_rate_kbps:
            print(f"     Bit-rate   : {as_.bit_rate_kbps} kbps")

    print(f"{sep}\n")
