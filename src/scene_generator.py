"""
src/scene_generator.py
----------------------
Generate scene images from VISUAL descriptions using Pollinations.ai (free, no API key).
Falls back to PIL generative art if the API is unreachable.
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from src.config import TEMP_DIR, get_setting
from src.logger import get_logger

log = get_logger(__name__)

# Pollinations.ai free image endpoint. An API key is OPTIONAL (works anonymously),
# but the admin-saved key is used when configured so it is NOT hardcoded.
_POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"
_ANON_RATE_LIMIT_SECS = 16  # ~1 req per 15s, add 1s buffer


def _get_pollinations_image_key() -> str:
    """Admin-saved Pollinations key (empty == anonymous free tier)."""
    return (get_setting("pollinations_api_key", "") or "").strip()

# Image cache directory
_CACHE_DIR = TEMP_DIR / "scene_cache"

# ── Consistent cinematic corporate style (drives style continuity across scenes) ──
# Appended to EVERY visual prompt so all scenes in one video share a coherent
# look, matching the reference "Google Flow"-style high-production corporate/tech
# grade instead of a random collage of mismatched styles.
_DEFAULT_STYLE_SUFFIX = (
    "cinematic corporate tech commercial, sleek futuristic UI, clean high-end studio lighting, "
    "premium product photography, deep navy and cyan accent grade, shallow depth of field, "
    "professional CGI render, high production value, 4k"
)


def build_visual_prompt(visual_description: str, style_suffix: Optional[str] = None) -> str:
    """Combine a scene's specific VISUAL description with the consistent style suffix.

    If no style is provided, the default cinematic corporate suffix is used.
    The specific per-scene content ALWAYS comes first (never replaced by a generic
    keyword), then the shared style grade is appended for continuity.
    """
    specific = (visual_description or "").strip()
    style = (style_suffix or "").strip() or _DEFAULT_STYLE_SUFFIX
    if not specific:
        return style
    return f"{specific.rstrip(', ')}, {style}"


def _cache_key(prompt: str, width: int, height: int) -> str:
    """Deterministic cache key from prompt + dimensions."""
    raw = f"{prompt.lower().strip()}|{width}x{height}"
    return hashlib.md5(raw.encode()).hexdigest()


def _fetch_scene_image(full_prompt: str, width: int, height: int, seed: int, timeout: int) -> Optional[bytes]:
    """Download one scene image bytes blob from Pollinations (no retry).

    Uses the admin-saved Pollinations key when configured (token param), else
    the anonymous free tier. Not hardcoded — respects admin settings.
    """
    encoded_prompt = urllib.parse.quote(full_prompt)
    params = f"?width={width}&height={height}&nologo=true"
    token = _get_pollinations_image_key()
    if token:
        params += f"&token={urllib.parse.quote(token)}"
    if seed is not None:
        params += f"&seed={seed}"
    url = _POLLINATIONS_URL.format(prompt=encoded_prompt) + params

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Vergeclip/1.0",
            "Accept": "image/jpeg,image/png,image/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()

    if len(data) < 1000:
        log.warning("Scene gen: response too small (%d bytes)", len(data))
        return None
    return data


def generate_scene_image(
    visual_description: str,
    width: int = 1080,
    height: int = 1920,
    seed: Optional[int] = None,
    output_path: Optional[Path] = None,
    timeout: int = 45,
    style_suffix: Optional[str] = None,
    retries: int = 1,
) -> Optional[Path]:
    """
    Generate a single scene image from a full visual description.

    The full per-scene VISUAL prompt is sent to the model (never a generic
    keyword), with a consistent corporate style suffix appended for continuity.
    On API failure this retries `retries` more times with a slightly adjusted
    prompt. FAILURES ARE LOGGED LOUDLY — there is NO silent success fallback here
    (the caller decides what to do when this returns None). Returns a cached Path
    on success or None after retries are exhausted.
    """
    if not visual_description or not visual_description.strip():
        log.warning("Scene gen: empty visual description, skipping")
        return None

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    full_prompt = build_visual_prompt(visual_description, style_suffix)
    seed = seed if seed is not None else 42
    key_mode = "admin key" if _get_pollinations_image_key() else "anonymous free"
    log.info("Scene gen: using Pollinations image (%s) -> '%s'", key_mode, visual_description[:50])

    # Check cache first (keyed on the FULL augmented prompt so style changes bust it)
    ck = _cache_key(full_prompt, width, height)
    cached = _CACHE_DIR / f"{ck}.jpg"
    if cached.exists() and cached.stat().st_size > 1000:
        log.info("Scene cache hit: %s (%.1f KB)", ck, cached.stat().st_size / 1024)
        if output_path:
            import shutil
            shutil.copy2(str(cached), str(output_path))
            return output_path
        return cached

    data = None
    attempts = retries + 1
    prompt_variants = [full_prompt] + [
        f"{full_prompt}, high detail, vivid composition (v{ri + 1})"
        for ri in range(min(retries, 2))
    ]
    for ai, pv in enumerate(prompt_variants[:attempts]):
        log.info("Scene gen [%d/%d]: downloading image for '%s'", ai + 1, attempts, visual_description[:50])
        try:
            data = _fetch_scene_image(pv, width, height, seed + ai, timeout)
            if data:
                break
        except Exception as e:
            log.warning("Scene gen [%d/%d] FAILED for '%s': %s", ai + 1, attempts, visual_description[:40], e)

    if not data:
        # LOUD failure — never treated as success by the caller.
        log.error("Scene generation FAILED after %d attempts for '%s' (will not use decorative template silently)", attempts, visual_description[:40])
        return None

    # Save to cache
    cached.write_bytes(data)
    log.info("Scene gen: saved %d bytes for '%s' (style-consistent)", len(data), visual_description[:40])

    if output_path:
        import shutil
        shutil.copy2(str(cached), str(output_path))
        return output_path
    return cached


def generate_scene_images_batch(
    visual_descriptions: list[str],
    width: int = 1080,
    height: int = 1920,
    progress_cb=None,
    style_suffix: Optional[str] = None,
    retries: int = 1,
) -> list[Optional[Path]]:
    """
    Generate multiple scene images, respecting Pollinations rate limits.
    Returns list of Paths (None for failed generations).
    Downloads sequentially with rate-limit delay between requests.
    """
    if not visual_descriptions:
        return []

    results: list[Optional[Path]] = [None] * len(visual_descriptions)
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    for i, desc in enumerate(visual_descriptions):
        if not desc or not desc.strip():
            continue

        if progress_cb:
            pct = int((i / max(1, len(visual_descriptions))) * 100)
            progress_cb(f"Generating scene image {i + 1}/{len(visual_descriptions)} (VISUAL)...", pct)

        out = _CACHE_DIR / f"scene_{i:03d}.jpg"
        result = generate_scene_image(
            desc, width=width, height=height, seed=i * 1000 + 42, output_path=out,
            style_suffix=style_suffix, retries=retries,
        )
        results[i] = result

        # Rate-limit delay (skip after last image)
        if i < len(visual_descriptions) - 1 and result is not None:
            time.sleep(_ANON_RATE_LIMIT_SECS)

    return results


def generate_scene_images_parallel(
    visual_descriptions: list[str],
    width: int = 1080,
    height: int = 1920,
    progress_cb=None,
) -> list[Optional[Path]]:
    """
    Generate scene images with limited parallelism (2 concurrent).
    Still respects rate limits via staggered starts.
    """
    if not visual_descriptions:
        return []

    results: list[Optional[Path]] = [None] * len(visual_descriptions)
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _gen_one(idx: int, desc: str) -> tuple[int, Optional[Path]]:
        if not desc or not desc.strip():
            return idx, None
        out = _CACHE_DIR / f"scene_{idx:03d}.jpg"
        result = generate_scene_image(
            desc, width=width, height=height, seed=idx * 1000 + 42, output_path=out,
        )
        return idx, result

    # Stagger requests to respect rate limit: first 2 immediately, then one every 15s
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {}
        for i, desc in enumerate(visual_descriptions):
            if not desc or not desc.strip():
                continue
            # Stagger: submit in batches respecting rate limit
            if i >= 2:
                time.sleep(_ANON_RATE_LIMIT_SECS)
            f = pool.submit(_gen_one, i, desc)
            futures[f] = i

        for f in as_completed(futures):
            idx, result = f.result()
            results[idx] = result
            if progress_cb:
                done = sum(1 for r in results if r is not None)
                progress_cb(f"Scene {done}/{len(visual_descriptions)} ready", int(done / max(1, len(visual_descriptions)) * 100))

    return results
