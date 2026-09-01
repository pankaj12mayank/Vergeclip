"""
src/video_generator.py
----------------------
Generate short video clips from text prompts.
Supports:
  1. Pollinations.ai (Veo/Wan models) — paid, requires credits (best quality)
  2. Agnes AI Video V2.0 — completely free, no usage limits
  3. Local CogVideoX via ComfyUI (offline, uses your GPU)

Falls back to PIL generative art if no provider succeeds.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

from src.config import TEMP_DIR, get_setting
from src.logger import get_logger

log = get_logger(__name__)

_CACHE_DIR = TEMP_DIR / "video_cache"


def _cache_key(prompt: str, provider: str, duration: int) -> str:
    raw = f"{provider}|{prompt.lower().strip()}|{duration}s"
    return hashlib.md5(raw.encode()).hexdigest()


def _get_pollinations_key() -> Optional[str]:
    """Get Pollinations API key from admin settings."""
    return get_setting("pollinations_api_key", "")


def _get_agnes_key() -> Optional[str]:
    """Get Agnes AI API key from admin settings."""
    return get_setting("agnes_api_key", "")


def _get_comfyui_url() -> Optional[str]:
    """Get ComfyUI server URL from admin settings."""
    return get_setting("comfyui_url", "http://127.0.0.1:8188")


# ── Pollinations.ai Video ─────────────────────────────────────────────────────

def _pollinations_generate(
    prompt: str,
    duration: int = 5,
    width: int = 1080,
    height: int = 1920,
    model: str = "wan-fast",
    timeout: int = 120,
) -> Optional[bytes]:
    """
    Generate video via Pollinations.ai gen.pollinations.ai/video endpoint.
    Returns MP4 bytes or None.
    """
    api_key = _get_pollinations_key()
    if not api_key:
        log.warning("Pollinations: no API key configured")
        return None

    encoded = urllib.parse.quote(prompt.strip())
    params = f"?model={model}&duration={duration}"
    url = f"https://gen.pollinations.ai/video/{encoded}{params}"

    try:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Vergeclip/1.0",
                "Accept": "video/mp4",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()

        if len(data) < 5000:
            log.warning("Pollinations: response too small (%d bytes)", len(data))
            return None

        log.info("Pollinations: generated %d bytes for '%s'", len(data), prompt[:50])
        return data

    except Exception as e:
        log.warning("Pollinations video failed: %s", e)
        return None


# ── Agnes AI Video (FREE, unlimited) ───────────────────────────────────────────

def _agnes_generate(
    prompt: str,
    duration: int = 5,
    width: int = 1080,
    height: int = 1920,
    timeout: int = 180,
) -> Optional[bytes]:
    """
    Generate video via Agnes AI Video V2.0 API — completely free, no usage limits.
    Async: create task → poll for result → download video.
    Returns MP4 bytes or None.
    """
    api_key = _get_agnes_key()
    if not api_key:
        log.warning("Agnes AI: no API key configured")
        return None

    # Create video task
    payload = json.dumps({
        "model": "agnes-video-v2.0",
        "prompt": prompt.strip(),
        "width": width,
        "height": height,
        "frame_rate": 24,
    }).encode("utf-8")

    create_url = "https://apihub.agnes-ai.com/v1/videos"

    try:
        req = urllib.request.Request(
            create_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Vergeclip/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())

        video_id = result.get("video_id") or result.get("id") or result.get("task_id")
        if not video_id:
            log.warning("Agnes AI: no video_id in response: %s", str(result)[:200])
            return None

        log.info("Agnes AI: task created, video_id=%s — polling...", video_id)

        # Poll for completion
        poll_url = f"https://apihub.agnes-ai.com/agnesapi?video_id={video_id}"
        for _ in range(120):  # up to 4 min
            time.sleep(2)
            try:
                poll_req = urllib.request.Request(
                    poll_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "User-Agent": "Vergeclip/1.0",
                    },
                )
                with urllib.request.urlopen(poll_req, timeout=30) as poll_resp:
                    status = json.loads(poll_resp.read().decode())
            except Exception as e:
                log.warning("Agnes AI poll error: %s", e)
                continue

            task_status = status.get("status", "")
            if task_status == "completed":
                video_url = status.get("url") or status.get("metadata", {}).get("url")
                if not video_url:
                    log.warning("Agnes AI: completed but no url: %s", str(status)[:200])
                    return None
                # Download video
                dl_req = urllib.request.Request(video_url, headers={"User-Agent": "Vergeclip/1.0"})
                with urllib.request.urlopen(dl_req, timeout=60) as dl_resp:
                    data = dl_resp.read()
                if len(data) < 5000:
                    log.warning("Agnes AI: video too small (%d bytes)", len(data))
                    return None
                log.info("Agnes AI: generated %d bytes for '%s'", len(data), prompt[:50])
                return data
            elif task_status == "failed":
                log.warning("Agnes AI task failed: %s", str(status)[:200])
                return None
            # else still processing, keep polling

        log.warning("Agnes AI: task timed out after polling")
        return None

    except Exception as e:
        log.warning("Agnes AI video failed: %s", e)
        return None


# ── Local CogVideoX via ComfyUI (offline, GPU) ──────────────────────────────────

def _comfyui_generate(
    prompt: str,
    duration: int = 5,
    width: int = 480,
    height: int = 720,
    timeout: int = 600,
) -> Optional[bytes]:
    """
    Generate a video locally via a ComfyUI server (http://127.0.0.1:8188).
    Requires ComfyUI running with CogVideoX loaded — fastest offline option
    for a 6GB GPU. Returns MP4 bytes or None.
    """
    comfy_url = get_setting("comfyui_url", "http://127.0.0.1:8188").strip().rstrip("/")
    if not comfy_url:
        log.warning("ComfyUI: no URL configured")
        return None

    # Check ComfyUI is reachable
    try:
        req = urllib.request.Request(f"{comfy_url}/system_stats", headers={"User-Agent": "Vergeclip/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            stats = json.loads(resp.read().decode())
    except Exception as e:
        log.warning("ComfyUI not reachable at %s: %s", comfy_url, e)
        return None

    # Determine available CogVideo models
    avail_models = []
    obj_info = {}
    try:
        req = urllib.request.Request(f"{comfy_url}/object_info", headers={"User-Agent": "Vergeclip/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            obj_info = json.loads(resp.read().decode())
        chk = obj_info.get("CheckpointLoaderSimple", {}).get("input", {}).get("required", {}).get("ckpt_name", {}).get(0, [])
        avail_models = [m for m in chk if "cogvideo" in m.lower() or "cog" in m.lower()]
    except Exception:
        pass

    # Prefer a user-provided API-format workflow (cogvideo_workflow_api.json) so
    # CogVideo node graphs that vary by ComfyUI install don't break generation.
    api_workflow_path = _CACHE_DIR.parent / "comfyui_workflow_api.json"
    if api_workflow_path.exists():
        try:
            api_workflow = json.loads(api_workflow_path.read_text(encoding="utf-8"))
            log.info("ComfyUI: using API-format workflow from %s", api_workflow_path)
            return _run_comfyui_workflow(
                comfy_url, api_workflow, prompt,
                width=width, height=height, timeout=timeout,
            )
        except Exception as e:
            log.warning("ComfyUI: failed to use API workflow, falling back: %s", e)

    if not avail_models:
        log.warning("ComfyUI reachable but no CogVideo model found (%s). Models: %s", comfy_url, avail_models)
        return None

    ckpt_name = avail_models[0]
    log.info("ComfyUI: using local model '%s'", ckpt_name)

    # Build a minimal CogVideoX text-to-video workflow — uses EmptyLatentVideo +
    # CogVideo nodes if available, otherwise falls back to the SD image graph
    # (lower fidelity but still attempt offline rendering).
    seed = int(time.time() % 100000)
    has_video_loader = any(
        c in obj_info for c in ("CogVideoXTransformer3DModel", "LoadCogVideoXModel")
    )
    workflow = {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": ckpt_name},
        },
    }
    if has_video_loader and "EmptyLatentVideo" in obj_info:
        workflow.update({
            "3": {
                "class_type": "KSampler",
                "inputs": {"cfg": 3.5, "denoise": 1.0, "latent_image": ["5", 0], "model": ["4", 0], "negative": ["6", 0], "positive": ["7", 0], "sampler_name": "euler", "scheduler": "normal", "seed": seed, "steps": 30},
            },
            "5": {
                "class_type": "EmptyLatentVideo",
                "inputs": {"batch_size": 1, "height": height, "width": width, "length": 49, "frame_rate": 16},
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {"clip": ["4", 1], "text": "low quality, worst quality"},
            },
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {"clip": ["4", 1], "text": prompt},
            },
            "8": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
            },
            "9": {
                "class_type": "SaveVideo",
                "inputs": {"images": ["8", 0], "filename_prefix": "vergeclip", "frame_rate": 16},
            },
        })
    else:
        workflow.update({
            "3": {
                "class_type": "KSampler",
                "inputs": {"cfg": 3.5, "denoise": 1.0, "latent_image": ["5", 0], "model": ["4", 0], "negative": ["6", 0], "positive": ["7", 0], "sampler_name": "euler", "scheduler": "normal", "seed": seed, "steps": 30},
            },
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {"batch_size": 1, "height": height, "width": width},
            },
            "6": {
                "class_type": "CLIPTextEncode",
                "inputs": {"clip": ["4", 1], "text": "low quality, worst quality"},
            },
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {"clip": ["4", 1], "text": prompt},
            },
            "8": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
            },
            "9": {
                "class_type": "SaveVideo",
                "inputs": {"images": ["8", 0], "filename_prefix": "vergeclip", "frame_rate": 16},
            },
        })

    return _run_comfyui_workflow(comfy_url, workflow, prompt, width=width, height=height, timeout=timeout)


def _run_comfyui_workflow(comfy_url, workflow, prompt, width=480, height=720, timeout=600):
    """Queue a ComfyUI API-format workflow and download the rendered video."""
    try:
        seed = int(time.time() % 100000)
        wf_text = json.dumps(workflow)
        for token, value in (("{prompt}", prompt), ("{width}", width), ("{height}", height), ("{seed}", seed)):
            wf_text = wf_text.replace(token, str(value))
        workflow = json.loads(wf_text)
        req = urllib.request.Request(
            f"{comfy_url}/prompt",
            data=json.dumps({"prompt": workflow, "client_id": "vergeclip"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Vergeclip/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            log.warning("ComfyUI: no prompt_id: %s", str(result)[:200])
            return None

        # Poll history until done
        for _ in range(int(timeout / 2)):
            time.sleep(2)
            try:
                hist_url = f"{comfy_url}/history/{prompt_id}"
                req = urllib.request.Request(hist_url, headers={"User-Agent": "Vergeclip/1.0"})
                with urllib.request.urlopen(req, timeout=10) as hresp:
                    hist = json.loads(hresp.read().decode())
            except Exception:
                continue
            if prompt_id in hist:
                outputs = hist[prompt_id].get("outputs", {})
                for node_id, out in outputs.items():
                    for media_key in ("gifs", "videos", "images", "animated_gifs"):
                        for item in out.get(media_key, []):
                            filename = item.get("filename")
                            subfolder = item.get("subfolder", "")
                            _type = item.get("type", "output")
                            if not filename:
                                continue
                            dl = urllib.request.Request(
                                f"{comfy_url}/view?filename={urllib.parse.quote(filename)}&subfolder={urllib.parse.quote(subfolder)}&type={urllib.parse.quote(_type)}",
                                headers={"User-Agent": "Vergeclip/1.0"},
                            )
                            with urllib.request.urlopen(dl, timeout=120) as vid:
                                data = vid.read()
                            if len(data) > 5000:
                                log.info("ComfyUI: generated %d bytes via %s for '%s'", len(data), media_key, prompt[:50])
                                return data
                            log.warning("ComfyUI: output too small (%d bytes)", len(data))
                            return None

        log.warning("ComfyUI: task timed out")
        return None

    except Exception as e:
        log.warning("ComfyUI video failed: %s", e)
        return None


# ── Public API ────────────────────────────────────────────────────────────────

def generate_video_clip(
    prompt: str,
    duration: int = 5,
    width: int = 1080,
    height: int = 1920,
    provider: Optional[str] = None,
    output_path: Optional[Path] = None,
    timeout: int = 180,
) -> Optional[Path]:
    """
    Generate a short video clip from a text prompt.
    Tries active provider, falls back to PIL art.
    Returns Path to MP4 file or None.
    """
    if not prompt or not prompt.strip():
        return None

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Check cache — use generic key since we try multiple providers
    ck = _cache_key(prompt, "auto", duration)
    cached = _CACHE_DIR / f"{ck}.mp4"
    if cached.exists() and cached.stat().st_size > 5000:
        log.info("Video cache hit: %s", ck[:12])
        if output_path:
            import shutil
            shutil.copy2(str(cached), str(output_path))
            return output_path
        return cached

    # Try provider with fallback chain. The admin-chosen active provider is
    # tried FIRST so switching in the UI takes effect immediately, then we
    # fall through to any other configured providers, then local, then PIL.
    data = None
    pk = _get_pollinations_key()
    ak = _get_agnes_key()
    active = get_setting("video_gen_provider", "").strip().lower()

    # Build ordered provider list: [active] + [others by key] + [local]
    ordered = []
    available = []
    if pk:
        available.append("pollinations")
    if ak:
        available.append("agnes")
    available.append("local")  # always try local GPU if available

    if active in available:
        ordered.append(active)
        ordered.extend(p for p in available if p != active)
    else:
        ordered = list(available)

    for p in ordered:
        if p == "pollinations":
            data = _pollinations_generate(prompt, duration, width, height, timeout=timeout)
        elif p == "agnes":
            data = _agnes_generate(prompt, duration, width, height, timeout=timeout)
        elif p == "local":
            data = _comfyui_generate(prompt, duration, 480, 720, timeout=600)
        if data:
            log.info("Video generated via %s for '%s'", p, prompt[:50])
            break
        else:
            log.info("Video gen failed via %s, trying next provider...", p)

    if data is None:
        return None

    # Save to cache
    cached.write_bytes(data)
    if output_path:
        import shutil
        shutil.copy2(str(cached), str(output_path))
        return output_path
    return cached


def generate_video_clips_batch(
    visual_descriptions: list[str],
    duration: int = 5,
    width: int = 1080,
    height: int = 1920,
    provider: Optional[str] = None,
    progress_cb=None,
) -> list[Optional[Path]]:
    """
    Generate multiple video clips from visual descriptions.
    Respects rate limits between requests.
    """
    if not visual_descriptions:
        return []

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    results: list[Optional[Path]] = []
    log.info("Batch video gen: scenes=%d", len(visual_descriptions))

    for i, desc in enumerate(visual_descriptions):
        if not desc or not desc.strip():
            results.append(None)
            continue

        if progress_cb:
            pct = int((i / max(1, len(visual_descriptions))) * 100)
            progress_cb(f"Generating video scene {i + 1}/{len(visual_descriptions)}...", pct)

        out = _CACHE_DIR / f"scene_{i:03d}.mp4"
        result = generate_video_clip(
            desc, duration=duration, width=width, height=height,
            output_path=out,
        )
        results.append(result)

        # Rate limit between requests (only if we got a successful result)
        if i < len(visual_descriptions) - 1 and result is not None:
            time.sleep(2)

    succeeded = sum(1 for r in results if r is not None)
    log.info("Batch video gen done: %d/%d succeeded", succeeded, len(visual_descriptions))
    return results
