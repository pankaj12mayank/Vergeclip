"""
src/template_backgrounds.py
----------------------------
Tier 3 Visual Fallback: Local motion-background template loader.

Discovers and loads motion-loop background templates from ``assets/templates/backgrounds/*/config.json``.
Guaranteed to work offline with zero external API calls or GPU dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from src.config import FFMPEG_BIN, PROJECT_ROOT, TEMP_DIR, get_setting
from src.logger import get_logger

log = get_logger(__name__)

TEMPLATES_DIR = PROJECT_ROOT / "assets" / "templates" / "backgrounds"
CACHE_DIR = TEMP_DIR / "template_bg_cache"


def discover_templates() -> list[dict]:
    """Scan assets/templates/backgrounds/ for valid template config.json files."""
    if not TEMPLATES_DIR.exists():
        return []
    templates = []
    for t_dir in TEMPLATES_DIR.iterdir():
        if not t_dir.is_dir():
            continue
        cfg_file = t_dir / "config.json"
        mp4_file = t_dir / "background.mp4"
        if cfg_file.exists() and mp4_file.exists():
            try:
                data = json.loads(cfg_file.read_text(encoding="utf-8"))
                data["dir"] = str(t_dir)
                data["mp4_path"] = str(mp4_file)
                templates.append(data)
            except Exception as e:
                log.warning("Template config error in %s: %s", t_dir.name, e)
    return sorted(templates, key=lambda x: x.get("template_id", ""))


def get_enabled_templates() -> list[dict]:
    """Return enabled template backgrounds based on DB setting override."""
    all_tpls = discover_templates()
    if not all_tpls:
        return []
    enabled_ids_raw = get_setting("enabled_template_backgrounds", None)
    if enabled_ids_raw is None or enabled_ids_raw == "":
        return all_tpls
    try:
        enabled_set = set(json.loads(enabled_ids_raw))
        # Explicit empty list means truly none enabled (solid fallback will be used)
        if len(enabled_set) == 0:
            return []
        # Sentinel __none__ also means none
        if enabled_set == {"__none__"}:
            return []
        filtered = [t for t in all_tpls if t["template_id"] in enabled_set]
        return filtered if filtered else all_tpls
    except Exception:
        return all_tpls


def get_template_background(duration: float = 5.0, seed: int = 0) -> Optional[Path]:
    """Return a Path to a looped/trimmed 9:16 template motion clip for the duration.

    Zero external API calls. Zero GPU required.
    """
    templates = get_enabled_templates()
    if not templates:
        log.error("Tier 3 Fallback: No template background assets found in %s", TEMPLATES_DIR)
        return None

    tpl = templates[seed % len(templates)]
    mp4_src = Path(tpl["mp4_path"])
    if not mp4_src.exists():
        log.error("Tier 3 Fallback: Motion background file missing: %s", mp4_src)
        return None

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    duration_rounded = round(duration, 2)
    cache_path = CACHE_DIR / f"bg_{tpl['template_id']}_{duration_rounded:.2f}s.mp4"

    if cache_path.exists() and cache_path.stat().st_size > 5000:
        return cache_path

    cmd = [
        FFMPEG_BIN, "-y",
        "-stream_loop", "-1",
        "-i", str(mp4_src),
        "-t", f"{duration_rounded:.3f}",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-an",
        str(cache_path),
    ]
    from src.ffmpeg_utils import run_ffmpeg
    try:
        res = run_ffmpeg(cmd, timeout=60)
        if res.returncode == 0 and cache_path.exists():
            log.info("Tier 3 Fallback: Prepared background '%s' (%.1fs)", tpl['template_id'], duration_rounded)
            return cache_path
        log.error("Tier 3 Fallback: FFmpeg background render failed: %s", res.stderr[-200:])
    except Exception as exc:
        log.error("Tier 3 Fallback: Exception during template generation: %s", exc)

    return mp4_src
