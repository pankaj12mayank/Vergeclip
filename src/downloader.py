"""
src/downloader.py
-----------------
Phase 1 module: Robust YouTube & Web Video Downloader with VideoSailor + yt-dlp Dual Engine.
Automatically selects VideoSailor API or yt-dlp high-speed fallback with real-time step logging.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Callable

from src.config import INPUT_DIR, VIDEOSAILOR_API_KEY
from src.logger import get_logger, log_system_event

log = get_logger(__name__)


def _is_valid_url(url: str) -> bool:
    """Validate that the URL looks like a supported web video link."""
    pattern = re.compile(
        r"^(https?://)?"
        r"(www\.)?"
        r"(youtube\.com|youtu\.be|"
        r"vimeo\.com|dailymotion\.com|"
        r"twitch\.tv|tiktok\.com|twitter\.com|x\.com|instagram\.com)"
        r"(/\S*)?$",
        re.IGNORECASE,
    )
    return bool(pattern.match(url.strip()))


def _extract_video_id(url: str) -> str:
    """Extract YouTube video ID or sanitized URL slug."""
    match = re.search(r"(?:v=|\/|youtu\.be\/|embed\/|shorts\/)([a-zA-Z0-9_-]{11})", url)
    if match:
        return match.group(1)
    return re.sub(r"[^\w\-]", "_", url)[:32]


def download_via_ytdlp(url: str, output_dir: Path, progress_cb: Optional[Callable[[str, int], None]] = None) -> Path:
    """
    Download video via yt-dlp with progress reporting.
    High-speed, automatic 1080p/720p stream selection and fallback.
    """
    import yt_dlp

    video_id = _extract_video_id(url)
    dest_template = str(output_dir / f"{video_id}.%(ext)s")
    target_mp4 = output_dir / f"{video_id}.mp4"

    log.info("[yt-dlp] Starting high-speed stream download for: %s", url)
    if progress_cb:
        progress_cb(f"📥 Connecting to YouTube media stream via yt-dlp...", 10)

    def _ytdl_hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            if total > 0:
                pct = int((downloaded / total) * 100)
                msg = f"📥 Downloading video stream: {pct}% ({downloaded / 1024 / 1024:.1f}MB / {total / 1024 / 1024:.1f}MB)"
                if progress_cb:
                    progress_cb(msg, min(25, 10 + int(pct * 0.15)))
        elif d.get("status") == "finished":
            if progress_cb:
                progress_cb("✓ Download stream completed, remuxing MP4...", 25)

    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": dest_template,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [_ytdl_hook],
        "nocheckcertificate": True,
        "ignoreerrors": False,
        "socket_timeout": 30,
    }

    # Pass ffmpeg location to yt-dlp so it can merge video+audio
    from src.config import FFMPEG_BIN
    import os as _os, shutil as _shutil
    if _os.path.isfile(FFMPEG_BIN):
        ffmpeg_dir = _os.path.dirname(FFMPEG_BIN)
        # imageio-ffmpeg names binary differently (ffmpeg-win-x86_64-v7.1.exe)
        # yt-dlp looks for 'ffmpeg.exe' in the dir — create a symlink if needed
        ffmpeg_link = _os.path.join(ffmpeg_dir, "ffmpeg.exe")
        if not _os.path.exists(ffmpeg_link):
            try:
                _shutil.copy2(FFMPEG_BIN, ffmpeg_link)
            except Exception:
                pass
        ydl_opts["ffmpeg_location"] = ffmpeg_dir

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(ydl.download, [url])
                try:
                    future.result(timeout=600)  # 10 minute total timeout including merge
                except concurrent.futures.TimeoutError:
                    log.error("[yt-dlp] Download timed out after 10 minutes")
                    raise RuntimeError("Video download timed out (10 min limit)")
        except Exception as e:
            log.error("[yt-dlp] Download failed: %s", e)
            raise RuntimeError(f"Failed to download video from YouTube: {e}")

    if progress_cb:
        progress_cb("✓ Download complete, finalizing...", 25)

    # Find the resulting file
    if target_mp4.exists():
        log.info("[yt-dlp] Video successfully saved to %s (%.2f MB)", target_mp4.name, target_mp4.stat().st_size / (1024 * 1024))
        return target_mp4

    # Look for any matching extension
    for cand in output_dir.glob(f"{video_id}.*"):
        if cand.is_file() and cand.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}:
            log.info("[yt-dlp] Video saved as %s", cand.name)
            return cand

    raise RuntimeError("yt-dlp finished download but target video file was not found in directory.")


def download_via_videosailor(url: str, output_dir: Path, api_key: str, progress_cb: Optional[Callable[[str, int], None]] = None) -> Path:
    """Download video via VideoSailor REST API."""
    video_id = _extract_video_id(url)
    dest_path = output_dir / f"{video_id}.mp4"

    log.info("[VideoSailor] Initiating download request for: %s", url)
    if progress_cb:
        progress_cb(f"⚡ Initiating cloud download via VideoSailor...", 10)

    init_url = "https://api.videosailor.com/api/download"
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
        "User-Agent": "curl/8.0.1",
    }
    payload = json.dumps({"url": url}).encode("utf-8")
    req = urllib.request.Request(init_url, data=payload, headers=headers, method="POST")

    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    download_url = data.get("downloadUrl")
    if not download_url:
        raise RuntimeError(f"VideoSailor returned invalid response: {data}")

    log.info("[VideoSailor] Streaming video to %s...", dest_path.name)
    stream_req = urllib.request.Request(download_url, headers={
        "X-API-Key": api_key,
        "User-Agent": "curl/8.0.1",
    })

    with urllib.request.urlopen(stream_req, timeout=300) as resp, open(dest_path, "wb") as f:
        bytes_downloaded = 0
        while True:
            chunk = resp.read(128 * 1024)
            if not chunk:
                break
            f.write(chunk)
            bytes_downloaded += len(chunk)
            if progress_cb and bytes_downloaded % (1024 * 1024) == 0:
                progress_cb(f"📥 Streaming cloud video: {bytes_downloaded / 1024 / 1024:.1f} MB", 20)

    log.info("[VideoSailor] Download finished → %s (%.2f MB)", dest_path.name, bytes_downloaded / (1024 * 1024))
    return dest_path


def download_video(url: str, output_dir: Optional[Path] = None, progress_cb: Optional[Callable[[str, int], None]] = None) -> Path:
    """
    Download *url* to *output_dir* using VideoSailor with automatic yt-dlp fallback.
    Guarantees that downloads never stall or fail silently.
    """
    url = url.strip()
    if not url:
        raise ValueError("Video URL must not be empty.")

    if not _is_valid_url(url):
        raise ValueError(
            f"The URL '{url}' does not look like a supported video link. "
            "Please provide a valid YouTube or video URL."
        )

    dest_dir = output_dir or INPUT_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    api_key = VIDEOSAILOR_API_KEY or os.environ.get("VIDEOSAILOR_API_KEY", "").strip()

    # Try VideoSailor first if key is configured
    if api_key:
        try:
            return download_via_videosailor(url, dest_dir, api_key, progress_cb)
        except Exception as vs_err:
            log.warning("VideoSailor failed (%s). Switching to yt-dlp engine...", vs_err)
            log_system_event("PIPELINE", "VideoSailor Fallback", f"VideoSailor failed: {vs_err}. Falling back to yt-dlp.", severity="WARN")
            if progress_cb:
                progress_cb("ℹ Cloud downloader busy. Using high-speed local engine...", 12)

    # Automatic yt-dlp Engine
    try:
        return download_via_ytdlp(url, dest_dir, progress_cb)
    except Exception as ytdl_err:
        log.error("yt-dlp download failed for %s: %s", url, ytdl_err)
        log_system_event("PIPELINE", "Video Download Error", f"Download failed for URL {url}: {ytdl_err}", severity="ERROR")
        raise RuntimeError(f"Failed to download video from YouTube: {ytdl_err}")
