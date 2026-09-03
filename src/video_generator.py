"""
src/video_generator.py
----------------------
Scene Generation (Script-to-Video) video clip generation.

Thin adapter over the provider implementations in ``src.scene_providers``.
It reads the ACTIVE scene provider from the ``scene_providers`` DB table at
runtime and delegates generation to that provider only (no silent cross-provider
fallback unless the caller requests a chain).

Providers available: local (Wan2.1/LTX via ComfyUI), fal.ai, Replicate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.logger import get_logger
from src.scene_providers import generate_scene, get_active_provider

log = get_logger(__name__)


def generate_video_clip(
    prompt: str,
    duration: int = 5,
    width: int = 1080,
    height: int = 1920,
    provider: Optional[str] = None,
    output_path: Optional[Path] = None,
    timeout: int = 180,
) -> Optional[Path]:
    """Generate one scene background clip with the active (or given) provider."""
    return generate_scene(
        prompt,
        duration=duration,
        width=width,
        height=height,
        provider_key=provider,
        output_path=output_path,
    )


def generate_video_clips_batch(
    visual_descriptions: list[str],
    duration: int = 5,
    width: int = 1080,
    height: int = 1920,
    provider: Optional[str] = None,
    progress_cb=None,
) -> list[Optional[Path]]:
    """Generate scene clips for every visual description, in order.

    Uses the ACTIVE provider for the whole batch. Local (offline GPU) renders
    sequentially to avoid queue thrash; cloud providers (fal/replicate) run a
    small number of concurrent requests.
    """
    if not visual_descriptions:
        return []

    total = len(visual_descriptions)
    results: list[Optional[Path]] = [None] * total
    log.info("Scene gen: %d scene(s), provider=%s", total, provider or "active")

    key = provider or ((get_active_provider() or {}).get("provider_key"))
    workers = 1 if key == "local" else min(4, max(1, min(4, total)))
    del key

    from src.scene_providers import _CACHE_DIR
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _clip_path(i):
        return _CACHE_DIR / f"scene_{i:03d}.mp4"

    def _gen(job):
        i, desc = job
        try:
            return i, generate_video_clip(
                desc, duration=duration, width=width, height=height,
                provider=provider, output_path=_clip_path(i),
            )
        except Exception as exc:
            log.error("Scene %d video gen raised: %s", i + 1, exc)
            return i, None

    from concurrent.futures import ThreadPoolExecutor, as_completed

    pending = [
        (i, desc) for i, desc in enumerate(visual_descriptions)
        if desc is not None and desc.strip()
    ]

    _done = [0]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_gen, job): job[0] for job in pending}
        for fut in as_completed(futures):
            i, path = fut.result()
            results[i] = path
            _done[0] += 1
            if progress_cb:
                pct = int((sum(1 for r in results if r is not None) / total) * 100)
                progress_cb(f"Generating video scene {_done[0]}/{len(pending)}...", min(99, pct))

    succeeded = sum(1 for r in results if r is not None)
    log.info("Scene gen done: %d/%d succeeded (provider=%s)", succeeded, total, provider or "active")
    return results
