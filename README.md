# Vergeclip AI 🎙️⚡ — Viral Shorts Generator

**Vergeclip AI** is an automated AI video pipeline that turns long-form podcasts, YouTube videos, or raw text topics into high-converting **vertical (9:16) short-form clips** with face tracking, kinetic word-by-word subtitles, semantic hook detection, and optional AI-generated cinematic scenes.

Everything needed to run the product is in this repo. A fresh `git clone` → `pip install -r requirements.txt` → `python server.py` gets a fully working server (verified against a clean Python 3.12 environment).

---

## Table of Contents
1. [Key Features](#-key-features)
2. [Architecture](#-architecture)
3. [Prerequisites](#-prerequisites)
4. [Installation](#-installation)
5. [Environment Configuration](#-environment-configuration)
6. [Running the Server](#-running-the-server)
7. [Default Credentials](#-default-credentials)
8. [Web UI & Admin](#-web-ui--admin)
9. [AI Providers & Local Video Generation](#-ai-providers--local-video-generation)
10. [Video Output & Pipeline Settings](#-video-output--pipeline-settings)
11. [API Reference Overview](#-api-reference-overview)
12. [Deployment](#-deployment)
13. [Troubleshooting](#-troubleshooting)

---

## ✨ Key Features

- **🎬 Dual Generation Modes**
  1. **YouTube URL → Shorts**: paste a link to auto-download, transcribe, score viral hooks, center active speakers in 9:16, and burn animated karaoke captions.
  2. **Topic → Viral Script (No Video Needed)**: provide a prompt/idea/question to generate retention-optimized short scripts with hooks, twists, and SEO metadata.
- **🧠 Production AI Pipelines (Editable in UI / Admin)** — hook ranking, face tracking, kinetic subtitles, titles & hashtags, topic-to-script, b-roll/visual pacing.
- **🎥 AI Scene Generation (Script-to-Video)**: optional cinematic agent-generated backgrounds per scene — via **Agnes AI (free cloud)**, **Pollinations.ai (paid)**, or **Local CogVideoX-2B** (your GPU through ComfyUI, fully offline).
- **📝 Real-Time System & User Audit Log Stream** with severity filtering and 1-click purge.
- **🤖 Custom AI Provider Support (OpenAI-Compatible)**: Google Gemini, OpenAI, Groq, DeepSeek, OpenRouter, vLLM, or local Ollama.
- **👑 Single Owner & Client Management**: owner account with unlimited quota, monthly allowance controls for standard users, guest-trial device reset.
- **🖼️ Glassmorphic Cyber Neon Theme** with offline guard protecting client sessions when the backend is down.

---

## 🏗️ Architecture

```
Vergeclip/
├─ server.py                 # FastAPI + Uvicorn production server (entry point)
├─ dev.py                    # Dev launcher / utilities
├─ requirements.txt          # Complete dependency manifest (fresh-install verified)
├─ Dockerfile / docker-compose.yml / render.yaml / nixpacks.toml / Procfile
├─ frontend/                 # index.html, admin.html, app.js, admin.js, app.css, auth.js
├─ src/                      # core engine (config, renderer, reframer, captions, video, tts, db, auth…)
├─ app/                      # pipeline modules (transcriber, clip_selector, semantic_ranker, main)
├─ input/  output/  temp/  logs/  data/  storage/   # created at startup
└─ comfyui/                  # OPTIONAL local CogVideoX runtime (its own venv — see below)
```

- **Startup**: `src/config.py` auto-creates the runtime directories and loads `.env`.
- **Persistence**: SQLite by default at `data/users.db` (Postgres-ready via `DATABASE_URL`).
- **Frontend**: plain HTML/JS/CSS served by FastAPI; Admin portal at `/admin.html`.

---

## 🛠️ Prerequisites

| Requirement | Notes |
| :--- | :--- |
| **Python 3.11 – 3.13** | 3.12 recommended (verified). |
| **FFmpeg** | `ffmpeg -version` on PATH (recommended). If missing, `imageio-ffmpeg` (auto-installed) bundles a binary as a fallback. |
| **API keys** | See [Environment Configuration](#-environment-configuration). Free options exist (Groq Whisper, Agnes AI). |
| **NVIDIA GPU (optional)** | Only needed for **Local CogVideoX** scene generation. Not required to run the app. |

---

## 📦 Installation

```bash
# 1. Clone the repository
git clone <your-repo-url> Vergeclip
cd Vergeclip

# 2. (Recommended) create a virtual environment
python -m venv .venv
# Windows:    .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate

# 3. Install all dependencies (verified clean-install on Python 3.12)
pip install -r requirements.txt

# 4. Copy and edit your environment file
cp .env.example .env
```

> Everything the product imports is listed in `requirements.txt`. A fresh venv + this single command installs the full stack (FastAPI, OpenCV, Pillow, yt-dlp, SQLAlchemy, JWT, faster-whisper, edge-tts, psutil, imageio-ffmpeg, providers…). **You do not need to install packages individually.**
>
> **Optional:** For local, offline GPU video generation (CogVideoX), the separate manifest is **`requirements-local.txt`** — install it into `comfyui/.venv` using `scripts/setup_comfyui_cogvideo.ps1` (see [Local CogVideoX](#local-cogvideox-2b-optional-offline)). It is *not* needed to run the main app.

---

## 🔐 Environment Configuration

Create a `.env` file in the project root (start from `.env.example`). All keys can also be set live from the **Admin Portal → Settings**.

```env
# ── Video Downloader (YouTube high-speed API) ──
VIDEOSAILOR_API_KEY=your_videosailor_key

# ── Transcription Provider ──
# Options: assemblyai (cloud paid) | groq (FREE whisper-large-v3) | faster_whisper (local/free)
TRANSCRIPTION_PROVIDER=groq
GROQ_API_KEY=your_groq_key
ASSEMBLYAI_API_KEY=your_assemblyai_key

# ── AI Ranking Provider (LLM) ──
# Options: gemini (Google, recommended) | openai | groq | ollama | deepseek | openrouter
RANKING_PROVIDER=gemini
GOOGLE_API_KEY=your_gemini_key
OPENAI_API_KEY=your_openai_key

# ── Auth / Security ──
# Generate:  python -c "import secrets; print(secrets.token_urlsafe(32))"
JWT_SECRET_KEY=change-me-to-a-strong-random-secret-at-least-32-chars
JWT_ALGORITHM=HS256
JWT_EXPIRE_MIN=10080
AUTH_REQUIRED=false
CORS_ORIGINS=*

# ── Server ──
HOST=0.0.0.0
PORT=5000
RELOAD=false

# ── Database (optional; SQLite is default) ──
# DATABASE_URL=sqlite:///./data/users.db
```

**Minimum to start (free):**
- `TRANSCRIPTION_PROVIDER=groq` + `GROQ_API_KEY` (free Whisper) **or** `faster_whisper` (runs locally, no key).
- `RANKING_PROVIDER=gemini` + `GOOGLE_API_KEY` (any Gemini key).
- `JWT_SECRET_KEY` (generate a strong secret).

`AUTH_REQUIRED=false` lets guests use the app without login (default system owner `admin` / `Admin@123`).

---

## ▶️ Running the Server

```bash
# Production (fast, thread-safe)
python server.py --host 0.0.0.0 --port 5000

# Development (auto-reload)
python server.py --reload
# or
uvicorn server:app --reload --port 5000
```

**URLs after startup:**
- **Web App**: `http://localhost:5000/`
- **Admin Portal**: `http://localhost:5000/admin.html`
- **API Docs (Swagger)**: `http://localhost:5000/docs`
- **Health Check**: `http://localhost:5000/api/health` → `{"status":"healthy",...}`

---

## 👤 Default Credentials

The system initializes a default **System Owner** account:
- **Username**: `admin`
- **Password**: `Admin@123`

Standard users self-register at `/signup.html` without admin approval.

---

## 🖥️ Web UI & Admin

- **`/`** — main app: paste a YouTube link or topic; view your generated shorts.
- **`/admin.html`** — full admin portal:
  - **Overview / Dashboard** — system status, jobs, audit log.
  - **API Settings** — configure all provider keys, ranking/transcription provider.
  - **General Settings** — system limits (free-tier monthly limit, max video duration, **max shorts per video** hard cap).
  - **Pipeline Configuration** — video output spec, clip selection, semantic ranking, caption rendering, auto-enhancement, scoring weights, AI prompts.
  - **Jobs / Video Library** — monitor generation jobs and browse rendered output.

---

## 🎬 AI Providers & Local Video Generation

Scene/short video generation supports three interchangeable providers (pick one in **Admin → Pipeline Configuration → Scene Generation**):

| Provider | Cost | Notes |
| :--- | :--- | :--- |
| **Agnes AI V2.0** | Free | Cloud, unlimited-ish. Set `AGNES_API_KEY` in admin. |
| **Pollinations.ai** | Paid credits | Best-quality cloud output. |
| **Local CogVideoX-2B** | Free / offline | Runs on your own GPU through **ComfyUI** (`127.0.0.1:8188`). |

### Local CogVideoX-2B (optional, offline)
This uses ComfyUI with the kijai **ComfyUI-CogVideoXWrapper** node and the small **2B** model (fits a 6GB GPU; RTX 3050 tested).

The local runtime has its own dependency manifest — **`requirements-local.txt`** (PyTorch CUDA 12.6 + diffusers/transformers/peft/torchsde/etc.). It installs into **ComfyUI's own venv** (`comfyui/.venv`), **not** the app venv — that's why it's separate from `requirements.txt`.

**One-shot A→Z setup (Windows, automatically installs ComfyUI + CUDA torch + models):**
```powershell
.\scripts\setup_comfyui_cogvideo.ps1
```
> Requires an NVIDIA GPU + driver with CUDA 12.6, Python 3.10–3.12 (ComfyUI needs ≤3.12), and Git. Flags: `-SkipPythonInstall`, `-SkipComfyInstall`, `-SkipModels`.

1. Launch ComfyUI on `127.0.0.1:8188` (keep the terminal open).
2. In **Admin → Pipeline → Scene Generation**, set provider to **Local CogVideoX-2B** (ComfyUI URL `http://127.0.0.1:8188`), then **Check ComfyUI / GPU**.
3. The system auto-submits the workflow (48 frames / 15 steps / your target resolution / seed) to ComfyUI and pulls back the clip.
4. Model files are downloaded by the setup script to:
   - `comfyui/models/CogVideo/CogVideo2B/` (transformer + vae + scheduler)
   - `comfyui/models/text_encoders/t5xxl_fp8_e4m3fn.safetensors`

> ComfyUI is a **separate, optional** GPU runtime — install it only for fully-offline AI scene/clip generation. The main app runs fine without it (use the cloud Agnes/Pollinations providers).

---

## 🎛️ Video Output & Pipeline Settings

Shorts are rendered to **exactly** the resolution configured in **Admin → Pipeline Configuration → Video Output Specification** (Width / Height / FPS). Both podcast shorts and Script-to-Video scenes respect this target — output fills the frame (no letterboxing/boxing).

Also configurable there:
- **Clip Selection** — min/max duration, top-N candidates, min score, separation, step size, overlap, distribution strategy.
- **Semantic Ranking** — pool size, min score, top-N, separation.
- **Caption Rendering** — font size, colors, outline, max width/Y position, padding & timing.
- **Auto Enhancement** — color filter, pitch shift, FFmpeg filter string.
- **Scoring Weights (Advanced)** — 28 heuristic weights.

All changes apply immediately and persist to the app config.

---

## 📡 API Reference Overview

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/api/health` | `GET` | System health check |
| `/api/auth/login` | `POST` | Login (users & admin) |
| `/api/auth/signup` | `POST` | Self-service registration |
| `/api/auth/me` | `GET` | Current session details |
| `/api/pipeline/auto-generate` | `POST` | 1-Click YouTube → 9:16 shorts |
| `/api/pipeline/generate-from-topic` | `POST` | AI topic → short script |
| `/api/admin/config` | `GET` / `POST` | API keys & provider config |
| `/api/admin/pipeline-config` | `GET` / `POST` | Full pipeline settings |
| `/api/admin/prompts` | `GET` / `PUT` | AI prompt pipelines editor |
| `/api/admin/config/test` | `POST` | 1-Click API key verification |
| `/api/admin/system-health` | `GET` | CPU/RAM/disk metrics |
| `/api/admin/audit` | `GET` / `DELETE` | Audit log stream & batch delete |
| `/api/admin/trials/reset` | `POST` | Purge guest device fingerprint tracking |

Full interactive docs at `/docs`.

---

## 🚢 Deployment

The repo includes ready-made deployment manifests (all use `requirements.txt`):

| Platform | File | Command / Note |
| :--- | :--- | :--- |
| **Docker** | `Dockerfile` | `docker build -t vergeclip . && docker run -p 5000:5000 vergeclip` |
| **Docker Compose** | `docker-compose.yml` | `docker-compose up -d` |
| **Render** | `render.yaml` | Blueprint (Docker image, port 5000, `/health`) |
| **Nixpacks / Railway** | `nixpacks.toml`, `Procfile` | `pip install -r requirements.txt`, run `server.py` |

The Dockerfile installs **FFmpeg** + OpenCV runtime libs, then `pip install -r requirements.txt`. Set `PORT`/`HOST` and your API keys via environment variables or a mapped `.env`.

> **Note:** The ComfyUI/Optional local-GPU runtime is **not** part of the default Docker/web deployment (it needs a GPU). Use the cloud providers (Agnes/Pollinations) in containers.

---

## 🛠️ Troubleshooting

| Symptom | Fix |
| :--- | :--- |
| `ModuleNotFoundError` on a fresh pull | Re-run `pip install -r requirements.txt` in an activated venv. All real deps are listed. |
| `faster-whisper` not installed | Listed in requirements; if you changed provider manually, run `pip install faster-whisper`. |
| FFmpeg not found | Install FFmpeg on PATH, or rely on the bundled `imageio-ffmpeg` fallback. |
| Black bars / letterboxed shorts | Set the exact Width/Height in **Admin → Pipeline → Video Output Specification**; output fills that resolution. |
| Admin says a provider has no key | Set the key in **Admin → API Settings** or `.env` and re-save. |
| Local CogVideoX fails | Confirm ComfyUI is running on `127.0.0.1:8188` and the models are downloaded (see [Local CogVideoX](#local-cogvideox-2b-optional-offline)). |
| Auth errors | Set `JWT_SECRET_KEY`; with `AUTH_REQUIRED=false` guests can use the app. |

---

© 2026 **Vergeclip AI**. Built with FastAPI, Whisper, OpenCV, FFmpeg, edge-tts & ComfyUI.
