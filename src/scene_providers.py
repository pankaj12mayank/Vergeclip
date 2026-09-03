"""
src/scene_providers.py
----------------------
Scene Generation (Script-to-Video) providers.

Every provider implements the same ``generate(prompt, duration, width, height)``
interface and reads its own configuration (API key, model, endpoint, timeout,
active flag) from the ``scene_providers`` DB table at call time — nothing is
hardcoded, and switching the active provider takes effect immediately without a
redeploy.

Providers:
  1. local     — Wan2.1 / LTX-Video rendered on your own GPU via a local ComfyUI
                 server (offline, free). Endpoint + model are configurable.
  2. fal       — fal.ai cloud video API (pay-as-you-go).
  3. replicate — Replicate cloud video API (pay-as-you-go).

Each provider returns MP4 bytes on success or None on failure. Callers must NOT
silently fall back to another provider unless an explicit fallback chain is
requested — a successful generation must come from the active provider.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from src.config import TEMP_DIR, get_setting
from src.logger import get_logger

log = get_logger(__name__)

_CACHE_DIR = TEMP_DIR / "scene_providers_cache"

_PROVIDER_KEYS = ("local", "fal", "replicate")


# ── Config access ─────────────────────────────────────────────────────────────

def list_provider_configs():
    """Return all scene provider rows (keys masked) for the admin UI."""
    from src.models import SessionLocal, SceneProvider
    db = SessionLocal()
    try:
        rows = db.query(SceneProvider).order_by(SceneProvider.id.asc()).all()
        return [
            {
                "id": r.id,
                "provider_key": r.provider_key,
                "name": r.name,
                "model_name": r.model_name,
                "endpoint": r.endpoint,
                "timeout_seconds": r.timeout_seconds,
                "is_active": bool(r.is_active),
                "api_key_masked": _mask_key(r.api_key),
            }
            for r in rows
        ]
    finally:
        db.close()


def get_active_provider() -> Optional[dict]:
    """Return the active scene provider config with its real (unmasked) key."""
    from src.models import SessionLocal, SceneProvider
    db = SessionLocal()
    try:
        row = db.query(SceneProvider).filter(SceneProvider.is_active == True).first()  # noqa: E712
        if not row:
            return None
        return {
            "id": row.id,
            "provider_key": row.provider_key,
            "name": row.name,
            "api_key": row.api_key or "",
            "model_name": row.model_name or "",
            "endpoint": row.endpoint or "",
            "timeout_seconds": row.timeout_seconds or 180,
            "is_active": True,
        }
    finally:
        db.close()


def get_provider_config(provider_key: str) -> Optional[dict]:
    """Return a single provider's config (with real key) by key."""
    try:
        from src.models import SessionLocal, SceneProvider
        db = SessionLocal()
        try:
            row = db.query(SceneProvider).filter(SceneProvider.provider_key == provider_key).first()
            if not row:
                return None
            return {
                "id": row.id,
                "provider_key": row.provider_key,
                "name": row.name,
                "api_key": row.api_key or "",
                "model_name": row.model_name or "",
                "endpoint": row.endpoint or "",
                "timeout_seconds": row.timeout_seconds or 180,
                "is_active": bool(row.is_active),
            }
        finally:
            db.close()
    except Exception as e:
        log.warning("Could not read provider config for '%s': %s", provider_key, e)
        return None


def _mask_key(key: Optional[str]) -> str:
    if not key:
        return ""
    if len(key) <= 12:
        return key[:4] + "********" + (key[-2:] if len(key) > 4 else "")
    return key[:8] + "********" + key[-4:]


# ── Shared cache helper ───────────────────────────────────────────────────────

def _cache_key(prompt: str, provider: str, duration: int) -> str:
    raw = f"{provider}|{prompt.lower().strip()}|{duration}s"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _write_clip(data: bytes, provider: str, prompt: str, duration: int,
                output_path: Optional[Path] = None) -> Optional[Path]:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = _CACHE_DIR / f"{_cache_key(prompt, provider, duration)}.mp4"
    cached.write_bytes(data)
    if output_path:
        output_path.write_bytes(data)
        return output_path
    return cached


def _http_json(url: str, *, method="GET", headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


# ── Shared provider interface ─────────────────────────────────────────────────

class SceneProviderBase(ABC):
    """Common interface every scene provider implements."""

    provider_key: str = ""

    def __init__(self, config: dict):
        self.config = config  # from get_provider_config()
        self.api_key = (config or {}).get("api_key", "") or ""
        self.model = (config or {}).get("model_name", "") or ""
        self.endpoint = ((config or {}).get("endpoint", "") or "").strip().rstrip("/")
        self.timeout = int((config or {}).get("timeout_seconds") or 180)

    @abstractmethod
    def generate(self, prompt: str, duration: int = 5,
                 width: int = 1080, height: int = 1920) -> Optional[bytes]:
        """Return MP4 bytes for the prompt, or None on failure."""

    def _check_key(self):
        if not self.api_key:
            log.warning("%s: no API key configured", self.provider_key)
            return False
        return True


# ── Local (Wan2.1 / LTX-Video via ComfyUI) ────────────────────────────────────

class LocalProvider(SceneProviderBase):
    """Render Wan2.1 / LTX-Video scenes on a local ComfyUI server (offline).

    Config:
      - endpoint: ComfyUI server URL (e.g. http://127.0.0.1:8188)
      - model_name: which Wan/LTX checkpoint/model ComfyUI should use
      - timeout_seconds: max time to wait for the render
    """

    provider_key = "local"

    def generate(self, prompt, duration=5, width=1080, height=1920):
        comfy_url = self.endpoint or get_setting("comfyui_url", "http://127.0.0.1:8188")
        # Wan/LTX renders are memory-hungry on consumer GPUs — render small and
        # let the renderer upscale to the final 9:16 spec.
        width = min(width, 480)
        height = min(height, 720)
        timeout = self.timeout or 700

        if not self._probe(comfy_url):
            log.warning("Local: ComfyUI not reachable at %s", comfy_url)
            return None

        obj_info = self._object_info(comfy_url)
        workflow, nodes_ok = self._build_workflow(obj_info, prompt, width, height)
        if not workflow:
            log.warning("Local: no Wan/LTX video nodes detected in ComfyUI at %s", comfy_url)
            return None

        data = self._run_comfyui(comfy_url, workflow, timeout)
        if data:
            log.info("Local: generated %d bytes for '%s' via ComfyUI (Wan/LTX)", len(data), prompt[:50])
        return data

    def _probe(self, url) -> bool:
        try:
            status, _ = _http_json(f"{url}/system_stats", headers={"User-Agent": "Vergeclip/1.0"}, timeout=5)
            return status == 200
        except Exception:
            return False

    def _object_info(self, url) -> dict:
        try:
            status, body = _http_json(f"{url}/object_info", headers={"User-Agent": "Vergeclip/1.0"}, timeout=8)
            return json.loads(body) if status == 200 else {}
        except Exception:
            return {}

    def _build_workflow(self, obj_info, prompt, width, height):
        """Build a Wan2.1/LTX text-to-video API workflow for the nodes available.

        Tries, in order: a user-provided Wan/LTX workflow file in the project
        root, then dynamic graphs for known Wan/LTX node families.
        """
        # Detect Wan/LTX nodes by substring (actual ComfyUI has e.g. LTXVImgToVideo, EmptyLTXVLatentVideo)
        has_wan = any("Wan" in k for k in obj_info)
        has_ltx = any("LTXV" in k or "LTX" in k for k in obj_info)
        known_wan = has_wan and any(k in obj_info for k in (
            "WanVideoModelLoader", "WanImageToVideo", "WanT2V", "WanVideoComposite", "WanVideoSampler"))
        known_ltx = has_ltx  # any LTX family present is sufficient for LTX T2V
        if not (known_wan or known_ltx or has_wan or has_ltx):
            return None, False
        try:
            wf = self._load_workflow_file()
            if wf:
                # Validate workflow nodes actually exist in this ComfyUI install
                # Our bundled wan_workflow_api.json uses WanVideoWrapper nodes (WanVideoModelLoader etc.)
                # which don't exist on this host (has UNETLoader/CLIPLoader + LTX/Wan native). Skip if missing.
                needed = {v.get("class_type") for v in wf.values() if isinstance(v, dict)}
                if needed.issubset(set(obj_info.keys())):
                    wf["_feasible"] = True
                    return wf, True
                else:
                    missing = needed - set(obj_info.keys())
                    log.warning("Local: bundled workflow needs %s — not in ComfyUI, using LTX fallback", missing)
        except Exception:
            pass
        # If LTX/Wan nodes present but bundled workflow not compatible, generate minimal native workflow
        # This ensures Local actually produces real AI video instead of falling back to Ken Burns background
        if has_ltx or has_wan:
            return self._build_ltx_fallback_workflow(obj_info, prompt, width, height), True
        return None, False

    def _build_ltx_fallback_workflow(self, obj_info, prompt, width, height):
        """Generate minimal LTX T2V API workflow using available ComfyUI LTX nodes.

        Uses real video generation (UNETLoader + CLIPLoader ltxv + EmptyLTXVLatentVideo + KSampler + VAEDecode)
        so Script-to-Video gets movie-like motion, not just a Ken Burns still.
        Width/height are snapped to LTX step 32.
        """
        # Snap to LTX required step 32 and clamp to reasonable 9:16 vertical
        w = max(256, min(1024, int(width) // 32 * 32))
        h = max(256, min(1920, int(height) // 32 * 32))
        # LTX length: frames, must be 8k+1 (e.g. 97 ~4s @24fps). Use 97 for ~4s, will be trimmed to duration.
        length = 97
        # Pick available checkpoint/vae/clip names from object_info if possible
        try:
            clip_opts = obj_info.get("CLIPLoader", {}).get("input", {}).get("required", {}).get("clip_name", [[], {}])[0]
            clip_name = next((c for c in clip_opts if "umt5" in c.lower()), clip_opts[0] if clip_opts else "umt5_xxl_fp8_e4m3fn_scaled.safetensors")
        except Exception:
            clip_name = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
        try:
            vae_opts = obj_info.get("VAELoader", {}).get("input", {}).get("required", {}).get("vae_name", [[], {}])[0]
            vae_name = vae_opts[0] if vae_opts else "wan_2.1_vae.safetensors"
        except Exception:
            vae_name = "wan_2.1_vae.safetensors"
        try:
            unet_opts = obj_info.get("UNETLoader", {}).get("input", {}).get("required", {}).get("unet_name", [[], {}])[0]
            unet_name = unet_opts[0] if unet_opts else "wan2.1_t2v_14B_bf16.safetensors"
        except Exception:
            unet_name = "wan2.1_t2v_14B_bf16.safetensors"
        return {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": unet_name, "weight_dtype": "default"}},
            "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip_name, "type": "ltxv"}},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "{prompt}", "clip": ["2", 0]}},
            "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
            "5": {"class_type": "EmptyLTXVLatentVideo", "inputs": {"width": w, "height": h, "length": length, "batch_size": 1}},
            "6": {"class_type": "VAELoader", "inputs": {"vae_name": vae_name}},
            "7": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["3", 0], "negative": ["4", 0], "latent_image": ["5", 0], "seed": "{seed}", "steps": 20, "cfg": 7, "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["6", 0]}},
            "9": {"class_type": "SaveWEBM", "inputs": {"images": ["8", 0], "filename_prefix": "vergeclip", "codec": "vp9", "fps": 24, "crf": 32}},
        }

    def _load_workflow_file(self):
        from src.config import PROJECT_ROOT
        candidates = [
            PROJECT_ROOT / "wan_workflow_api.json",
            PROJECT_ROOT / "comfyui_workflow_api.json",
            TEMP_DIR / "wan_workflow_api.json",
        ]
        for p in candidates:
            if p.exists():
                try:
                    return json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
        return None

    def _run_comfyui(self, url, workflow, timeout):
        try:
            seed = int(time.time() % 100000)
            wf_text = json.dumps(workflow if isinstance(workflow, dict) else {})
            for token, value in (("{prompt}", self._prompt_placeholder()), ("{seed}", seed)):
                wf_text = wf_text.replace(token, str(value))
            workflow = json.loads(wf_text)
            payload = json.dumps({"prompt": workflow, "client_id": "vergeclip"}).encode("utf-8")
            status, body = _http_json(
                f"{url}/prompt", method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "Vergeclip/1.0"},
                data=payload, timeout=15)
            if status != 200:
                log.warning("Local: ComfyUI /prompt returned %s: %s", status, body[:200])
                return None
            result = json.loads(body)
            prompt_id = result.get("prompt_id")
            if not prompt_id:
                return None
            return self._poll_history(url, prompt_id, timeout)
        except Exception as e:
            log.warning("Local: ComfyUI generate failed: %s", e)
            return None

    def _prompt_placeholder(self):
        return "{prompt}"

    def _poll_history(self, url, prompt_id, timeout):
        for _ in range(max(1, int(timeout / 3))):
            time.sleep(3)
            try:
                status, body = _http_json(
                    f"{url}/history/{prompt_id}", headers={"User-Agent": "Vergeclip/1.0"}, timeout=10)
                if status != 200:
                    continue
                hist = json.loads(body)
            except Exception:
                continue
            if prompt_id in hist:
                for node_id, out in hist[prompt_id].get("outputs", {}).items():
                    for media_key in ("gifs", "videos", "images", "animated_gifs"):
                        for item in out.get(media_key, []):
                            filename = item.get("filename")
                            if not filename:
                                continue
                            subfolder = item.get("subfolder", "")
                            _type = item.get("type", "output")
                            dl = f"{url}/view?filename={urllib.parse.quote(filename)}&subfolder={urllib.parse.quote(subfolder)}&type={urllib.parse.quote(_type)}"
                            try:
                                _, data = _http_json(dl, headers={"User-Agent": "Vergeclip/1.0"}, timeout=120)
                            except Exception:
                                continue
                            if len(data) > 5000:
                                return data
                            return None
        log.warning("Local: ComfyUI task timed out after %ss", timeout)
        return None


# ── fal.ai ────────────────────────────────────────────────────────────────────

class FalProvider(SceneProviderBase):
    """fal.ai cloud video generation (async queue API).

    Config:
      - api_key: fal.ai API key
      - model_name: fal-hosted model id, e.g. kuaishou/kling-video/v1/standard/text-to-video
      - endpoint: fal queue base URL (https://queue.fal.run)
    """

    provider_key = "fal"

    def generate(self, prompt, duration=5, width=1080, height=1920):
        if not self._check_key() or not self.model:
            log.warning("fal: missing API key or model")
            return None
        model = self.model.strip("/")
        base = self.endpoint or "https://queue.fal.run"
        create_url = f"{base}/{model}"
        payload = json.dumps({
            "prompt": prompt.strip(),
            "num_frames": max(1, min(100, int(round(duration * 16)))),
            "size": f"{width}x{height}",
            "num_inference_steps": 30,
        }).encode("utf-8")
        headers = {
            "Authorization": f"Key {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Vergeclip/1.0",
        }
        try:
            status, body = _http_json(create_url, method="POST", headers=headers, data=payload, timeout=60)
        except urllib.error.HTTPError as e:
            log.warning("fal: create failed HTTP %s: %s", e.code, e.read()[:200])
            return None
        except Exception as e:
            log.warning("fal: create failed: %s", e)
            return None
        if status not in (200, 201, 202):
            log.warning("fal: create returned %s: %s", status, body[:300])
            return None
        try:
            result = json.loads(body)
        except Exception:
            result = {}
        request_id = result.get("request_id") or result.get("id")
        if not request_id:
            log.warning("fal: no request_id in response: %s", body[:200])
            return None

        status_url = f"{create_url}/requests/{request_id}/status"
        timeout = self.timeout or 180
        for _ in range(max(1, int(timeout / 3))):
            time.sleep(3)
            try:
                _, sbody = _http_json(status_url, headers=headers, timeout=30)
                sdata = json.loads(sbody)
            except urllib.error.HTTPError as e:
                if e.code == 200:
                    continue
                log.warning("fal: status error HTTP %s", e.code)
                continue
            except Exception:
                continue
            state = sdata.get("status", "").lower()
            if state in ("completed", "succeeded", "done"):
                return self._download_output(sdata)
            if state in ("failed", "error", "cancelled"):
                log.warning("fal: task %s: %s", state, sdata.get("error") or sdata.get("detail") or "")
                return None
        log.warning("fal: task timed out after %ss", timeout)
        return None

    def _download_output(self, sdata):
        output = sdata.get("output")
        if isinstance(output, dict):
            url = output.get("video") or output.get("url")
        else:
            url = output if isinstance(output, str) else None
        if not url:
            # fal sometimes wraps in a list
            if isinstance(output, list) and output:
                url = output[0].get("url") if isinstance(output[0], dict) else output[0]
        if not url:
            log.warning("fal: completed but no output URL: %s", str(sdata)[:200])
            return None
        try:
            _, data = _http_json(url, headers={"User-Agent": "Vergeclip/1.0"}, timeout=120)
            if len(data) > 5000:
                return data
        except Exception as e:
            log.warning("fal: download failed: %s", e)
        return None


# ── Replicate ─────────────────────────────────────────────────────────────────

class ReplicateProvider(SceneProviderBase):
    """Replicate cloud video generation.

    Config:
      - api_key: Replicate API token
      - model_name: e.g. wan-video/wan-2.1-t2v-14b
      - endpoint: https://api.replicate.com/v1
    """

    provider_key = "replicate"

    def generate(self, prompt, duration=5, width=1080, height=1920):
        if not self._check_key() or not self.model:
            log.warning("replicate: missing API key or model")
            return None
        base = (self.endpoint or "https://api.replicate.com/v1").rstrip("/")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Vergeclip/1.0",
        }
        payload = json.dumps({
            "version": None,
            "input": {"prompt": prompt.strip(), "duration": duration, "width": width, "height": height},
        }).encode("utf-8")
        # First fetch the model version for text-to-video input.
        version = self._resolve_version(base + "/models/" + self.model, headers)
        if not version:
            log.warning("replicate: could not resolve model %s", self.model)
            return None
        payload = json.dumps({
            "input": {"prompt": prompt.strip(), "duration": duration, "width": width, "height": height},
            "version": version,
        }).encode("utf-8")
        try:
            status, body = _http_json(base + "/predictions", method="POST", headers=headers, data=payload, timeout=30)
        except urllib.error.HTTPError as e:
            log.warning("replicate: create failed HTTP %s: %s", e.code, e.read()[:200])
            return None
        except Exception as e:
            log.warning("replicate: create failed: %s", e)
            return None
        if status not in (200, 201):
            log.warning("replicate: create returned %s: %s", status, body[:300])
            return None
        try:
            pred = json.loads(body)
        except Exception:
            return None
        pred_id = pred.get("id")
        if not pred_id:
            return None

        timeout = self.timeout or 180
        for _ in range(max(1, int(timeout / 2))):
            time.sleep(2)
            try:
                _, sbody = _http_json(base + f"/predictions/{pred_id}", headers=headers, timeout=30)
                sdata = json.loads(sbody)
            except Exception:
                continue
            st = (sdata.get("status") or "").lower()
            if st == "succeeded":
                out = sdata.get("output")
                url = out if isinstance(out, str) else (out[0] if isinstance(out, list) and out else None)
                if not url:
                    return None
                try:
                    _, data = _http_json(url, headers={"User-Agent": "Vergeclip/1.0"}, timeout=120)
                    if len(data) > 5000:
                        return data
                except Exception as e:
                    log.warning("replicate: download failed: %s", e)
                return None
            if st in ("failed", "canceled"):
                log.warning("replicate: prediction %s: %s", st, sdata.get("error"))
                return None
        log.warning("replicate: prediction timed out after %ss", timeout)
        return None

    def _resolve_version(self, model_url, headers):
        try:
            status, body = _http_json(model_url, headers=headers, timeout=30)
            if status != 200:
                return None
            data = json.loads(body)
            return data.get("latest_version", {}).get("id")
        except Exception:
            return None


# ── Dispatcher ────────────────────────────────────────────────────────────────

def _build_provider(provider_key: str) -> Optional[SceneProviderBase]:
    config = get_provider_config(provider_key)
    if not config:
        return None
    if provider_key == "local":
        return LocalProvider(config)
    if provider_key == "fal":
        return FalProvider(config)
    if provider_key == "replicate":
        return ReplicateProvider(config)
    return None


def get_ordered_provider_keys(preferred_key: Optional[str] = None) -> list[str]:
    """Return ONLY the active/preferred provider — no hardcoded fallback chain.

    If a provider is active (Local / fal.ai / Replicate), only that one is tried.
    Cross-provider fallback is disabled per user requirement — failure goes to
    Tier 2 (Ken Burns image) / Tier 3 (template) via scene_fallback, not to another video provider.
    If nothing is active and no preferred_key given, returns empty list so caller
    can directly use image/template defaults.
    """
    active = preferred_key or ((get_active_provider() or {}).get("provider_key"))
    if not active:
        return []
    return [active]


def generate_scene(
    prompt: str,
    duration: int = 5,
    width: int = 1080,
    height: int = 1920,
    provider_key: Optional[str] = None,
    output_path: Optional[Path] = None,
) -> Optional[Path]:
    """Generate a scene background clip using ONLY the active provider.

    No cross-provider fallback — if the active provider fails, caller (scene_fallback)
    handles Tier 2/3. If no provider is active, returns None so fallback is used.
    """
    if not prompt or not prompt.strip():
        return None

    chain = get_ordered_provider_keys(provider_key)
    if not chain:
        log.warning("Scene generation: no active provider — will use image/template fallback")
        return None
    log.info("Scene generation: attempting active provider %s", chain)

    for key in chain:
        cfg = get_provider_config(key)
        if not cfg:
            continue
        # Skip cloud providers with missing key
        if key in ("fal", "replicate") and not cfg.get("api_key"):
            continue

        ck = _cache_key(prompt, key, duration)
        cached = _CACHE_DIR / f"{ck}.mp4"
        if cached.exists() and cached.stat().st_size > 5000:
            if output_path:
                output_path.write_bytes(cached.read_bytes())
                return output_path
            return cached

        provider = _build_provider(key)
        if not provider:
            continue

        try:
            log.info("Attempting video scene generation via '%s' provider...", key)
            data = provider.generate(prompt, duration, width, height)
            if data and len(data) > 5000:
                log.info("Scene generation via '%s' SUCCEEDED", key)
                return _write_clip(data, key, prompt, duration, output_path)
            log.warning("Provider '%s' returned invalid or empty data, trying next...", key)
        except Exception as e:
            log.warning("Provider '%s' failed with exception: %s. Trying next...", key, e)

    log.warning("All video scene providers failed for prompt '%s'", prompt[:50])
    return None


def generate_still_image(
    prompt: str,
    width: int = 1080,
    height: int = 1920,
    output_path: Optional[Path] = None,
) -> Optional[Path]:
    """Generate a 9:16 still image for Tier 2 fallback.

    Tries fal.ai/Replicate image endpoints if keys configured, or generates a local
    procedural 9:16 background image using PIL (guaranteed 0-network failure).
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ck = hashlib.md5(f"still_img|{prompt.strip().lower()}".encode("utf-8")).hexdigest()
    cached = _CACHE_DIR / f"{ck}.png"
    if cached.exists() and cached.stat().st_size > 2000:
        if output_path:
            output_path.write_bytes(cached.read_bytes())
            return output_path
        return cached

    # Attempt fal.ai still image if key is set
    fal_cfg = get_provider_config("fal")
    if fal_cfg and fal_cfg.get("api_key"):
        try:
            url = "https://queue.fal.run/fal-ai/flux/schnell"
            payload = json.dumps({"prompt": prompt, "image_size": "portrait_16_9"}).encode("utf-8")
            headers = {"Authorization": f"Key {fal_cfg['api_key']}", "Content-Type": "application/json"}
            status, body = _http_json(url, method="POST", headers=headers, data=payload, timeout=15)
            if status in (200, 201):
                res = json.loads(body)
                img_url = (res.get("images") or [{}])[0].get("url")
                if img_url:
                    _, data = _http_json(img_url, timeout=30)
                    if len(data) > 2000:
                        cached.write_bytes(data)
                        if output_path:
                            output_path.write_bytes(data)
                            return output_path
                        return cached
        except Exception as e:
            log.warning("fal.ai still image request failed: %s", e)

    # Guaranteed procedural PIL image fallback
    try:
        from PIL import Image, ImageDraw
        import random
        rng = random.Random(hash(prompt) % 100000)
        c1 = (rng.randint(10, 40), rng.randint(5, 25), rng.randint(20, 60))
        c2 = (rng.randint(40, 100), rng.randint(15, 50), rng.randint(80, 180))
        img = Image.new("RGB", (width, height), c1)
        draw = ImageDraw.Draw(img)
        for y in range(height):
            ratio = y / height
            r = int(c1[0] * (1 - ratio) + c2[0] * ratio)
            g = int(c1[1] * (1 - ratio) + c2[1] * ratio)
            b = int(c1[2] * (1 - ratio) + c2[2] * ratio)
            draw.line([(0, y), (width, y)], fill=(r, g, b))
        cx, cy = width // 2, height // 3
        for r_rad in range(350, 0, -5):
            alpha = int(35 * (1 - r_rad / 350))
            draw.ellipse([(cx - r_rad, cy - r_rad), (cx + r_rad, cy + r_rad)], outline=(min(255, c2[0] + alpha), min(255, c2[1] + alpha), min(255, c2[2] + alpha)))
        cached.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(cached), format="PNG")
        if output_path:
            output_path.write_bytes(cached.read_bytes())
            return output_path
        return cached
    except Exception as e:
        log.error("Failed procedural image generation: %s", e)
        return None
