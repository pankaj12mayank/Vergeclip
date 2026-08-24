"""
downloader.py
-------------
Phase 1 module: Download a YouTube video to the input/ directory using VideoSailor API.

Public API:
    download_video(url: str, output_dir: Optional[Path] = None) -> Path
        Downloads *url* using VideoSailor and returns the local Path of the saved file.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from src.config import (
    INPUT_DIR,
    VIDEOSAILOR_API_KEY,
)
from src.logger import get_logger

log = get_logger(__name__)


def _is_valid_url(url: str) -> bool:
    """Validate that the URL looks like a supported web video link."""
    pattern = re.compile(
        r"^(https?://)?"                        # optional scheme
        r"(www\.)?"                             # optional www
        r"(youtube\.com|youtu\.be|"            # YouTube domains
        r"vimeo\.com|dailymotion\.com|"        # other common hosts
        r"twitch\.tv|soundcloud\.com)"         # more hosts
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


def download_via_videosailor(url: str, output_dir: Path, api_key: str) -> Path:
    """
    Download YouTube video via VideoSailor REST API (Step 1: Initiate, Step 2: Stream tunnel).
    Completely bypasses YouTube bot/IP challenges on cloud servers.
    """
    video_id = _extract_video_id(url)
    dest_path = output_dir / f"{video_id}.mp4"

    log.info("[VideoSailor] Initiating download request for: %s", url)

    # Step 1: Initiate download — returns a one-time tunnel URL
    init_url = "https://api.videosailor.com/api/download"
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
        "User-Agent": "curl/8.0.1",
    }
    payload = json.dumps({"url": url}).encode("utf-8")
    req = urllib.request.Request(init_url, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        err_msg = err.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"VideoSailor API request failed (HTTP {err.code}): {err_msg}") from err
    except Exception as exc:
        raise RuntimeError(f"VideoSailor API connection failed: {exc}") from exc

    download_url = data.get("downloadUrl")
    if not download_url:
        raise RuntimeError(f"VideoSailor API returned invalid response: {data}")

    log.info("[VideoSailor] Received tunnel URL. Streaming video to %s...", dest_path.name)

    # Step 2: Stream the file
    stream_req = urllib.request.Request(download_url, headers={
        "X-API-Key": api_key,
        "User-Agent": "curl/8.0.1",
    })

    try:
        with urllib.request.urlopen(stream_req, timeout=300) as resp, open(dest_path, "wb") as f:
            bytes_downloaded = 0
            while True:
                chunk = resp.read(128 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                bytes_downloaded += len(chunk)
    except Exception as exc:
        if dest_path.exists():
            dest_path.unlink(missing_ok=True)
        raise RuntimeError(f"Error streaming video file from VideoSailor: {exc}") from exc

    log.info("[VideoSailor] Download finished → %s (%.2f MB)", dest_path.name, bytes_downloaded / (1024 * 1024))
    return dest_path


def download_video(url: str, output_dir: Optional[Path] = None) -> Path:
    """
    Download *url* to *output_dir* (defaults to INPUT_DIR) using VideoSailor API.

    Parameters
    ----------
    url:        YouTube (or other supported) video URL.
    output_dir: Override the destination directory. Defaults to INPUT_DIR.

    Returns
    -------
    Path to the downloaded video file (.mp4).

    Raises
    ------
    ValueError:   If *url* is empty or invalid, or if VIDEOSAILOR_API_KEY is not set.
    RuntimeError: If download fails.
    """
    url = url.strip()

    if not url:
        raise ValueError("URL must not be empty.")

    if not _is_valid_url(url):
        raise ValueError(
            f"The URL '{url}' does not look like a supported video link.\n"
            "Please provide a full YouTube URL, e.g.:\n"
            "  https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    api_key = VIDEOSAILOR_API_KEY or os.environ.get("VIDEOSAILOR_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "VideoSailor API key is missing. Please provide VIDEOSAILOR_API_KEY in your environment or .env file."
        )

    dest_dir = output_dir or INPUT_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    return download_via_videosailor(url, dest_dir, api_key)
