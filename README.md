# Vergeclip AI — Viral Shorts Generator

**Vergeclip AI** turns long-form podcasts, YouTube videos, or raw text topics into high-converting **vertical (9:16) short-form clips** with face tracking, kinetic word-by-word subtitles, semantic hook detection, and optional AI-generated cinematic scenes via Local Wan2.1 / LTX-Video (ComfyUI), fal.ai, or Replicate.

---

## Table of Contents

1. [Key Features](#-key-features)
2. [Quick Start (5 Minutes)](#-quick-start-5-minutes)
3. [Architecture](#-architecture)
4. [Prerequisites](#-prerequisites)
5. [Installation](#-installation)
6. [Environment Configuration](#-environment-configuration)
7. [Running the Server](#-running-the-server)
8. [Default Credentials](#-default-credentials)
9. [Web UI & Admin Portal](#-web-ui--admin-portal)
10. [AI Providers & Local Video Generation](#-ai-providers--local-video-generation)
11. [API Reference](#-api-reference)
12. [Deployment](#-deployment)
13. [Troubleshooting](#-troubleshooting)

---

## Key Features

- **YouTube URL → Shorts**: auto-download, transcribe, score viral hooks, center active speakers in 9:16, burn animated karaoke captions.
- **Topic → Viral Script**: AI generates retention-optimized short scripts with hooks, twists, and SEO metadata.
- **AI Scene Generation (Script-to-Video)**: cinematic agent-generated backgrounds per scene via **Local Wan2.1 / LTX-Video** (offline, GPU), **fal.ai** (cloud), or **Replicate** (cloud). Config lives in the database — switch providers with one click.
- **Pipeline Prompts**: every AI step (hook ranking, captions, topic-to-script, scene selection) has an editable prompt template in the Admin Portal.
- **Real-Time Audit Log** with severity filtering and batch purge.
- **Custom AI Providers**: Gemini, OpenAI, Groq, DeepSeek, OpenRouter, Ollama (local LLM), or any OpenAI-compatible endpoint.
- **Single Owner + Client Management**: unlimited quota for owner, monthly allowance controls for standard users, guest trial reset.
- **Offline Guard**: glassmorphic theme protects sessions when backend is unreachable.

---

## Quick Start (5 Minutes)

```bash
# 1. Clone & enter
git clone <repo-url> Vergeclip && cd Vergeclip

# 2. Create venv + install
python -m venv .venv && .venv\Scripts\activate   # Windows
# or: source .venv/bin/activate                    # macOS/Linux

pip install -r requirements.txt

# 3. Configure (minimum: free tier)
cp .env.example .env
# Edit .env: set JWT_SECRET_KEY, RANKING_PROVIDER=groq, GROQ_API_KEY, JWT_SECRET_KEY

# 4. Start server
python server.py

# 5. Open browser
#   App:    http://localhost:5000/
#   Admin:  http://localhost:5000/admin.html
#   Login:  admin / Admin@123
```

That's it — the full pipeline works with **free providers only** (Groq Whisper for transcription + Groq/OpenAI for ranking).

---

## Architecture

```
Vergeclip/
├─ server.py                   # FastAPI + Uvicorn entry point
├─ dev.py                       # Dev utilities
├─ requirements.txt             # Main app dependencies (verified clean-install)
├─ requirements-local.txt       # Wan2.1 / LTX-Video (ComfyUI) deps (installed into comfyui/.venv)
├─ .env / .env.example           # Environment configuration
├─ Dockerfile / docker-compose.yml / render.yaml / nixpacks.toml / Procfile
├─ frontend/                    # index.html, admin.html, app.js, admin.js, app.css, auth.js
├─ src/                         # config, models, logger, renderer, reframer, captions, video, tts
├─ app/                         # pipeline modules (transcriber, clip_selector, semantic_ranker, main)
├─ scripts/
│   └─ setup_comfyui_wan.ps1  # PowerShell: full ComfyUI + Wan2.1 / LTX-Video install
├─ comfyui/                     # ComfyUI runtime (optional, GPU-only, offline)
│   ├─ .venv/                   # ComfyUI's own Python venv (PyTorch CUDA)
│   ├─ main.py                  # ComfyUI server entry point
│   ├─ custom_nodes/
│   │   └─ ComfyUI-WanVideoWrapper/   # Wan2.1 custom node
│   └─ models/
│       ├─ Diffusion_Models/     # Wan2.1 / LTX-Video checkpoints
│       └─ text_encoders/        # T5 / UMt5 / CLIP text encoders
├─ input/  output/  temp/  logs/  data/  storage/   # created at startup
└─ comfyui_workflow_api.json    # Bundled Wan2.1 / LTX-Video workflow for API calls
```

- **Startup**: `src/config.py` auto-creates runtime directories and loads `.env`.
- **Persistence**: SQLite at `data/users.db` (Postgres-ready via `DATABASE_URL`).
- **Frontend**: plain HTML/JS/CSS served by FastAPI; Admin at `/admin.html`.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.11 – 3.13** | 3.12 recommended and verified. |
| **FFmpeg** | `ffmpeg -version` on PATH. If missing, `imageio-ffmpeg` (auto-installed) bundles a binary as fallback. |
| **API keys** | See [Environment Configuration](#-environment-configuration). Free options exist (Groq Whisper, Groq LLMs). |
| **NVIDIA GPU (optional)** | Only for **Local Wan2.1 / LTX-Video** scene generation. Not required for the app. |

---

## Installation

### Main App

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
# Edit .env with your keys
```

All imports are in `requirements.txt`. A single `pip install` gets the full stack. No extra steps.

### Optional: Local Wan2.1 / LTX-Video (Offline GPU Scene Generation)

**Two ways to install:**

**Option A — From Admin UI (recommended, automatic):**
1. Start the Vergeclip server: `python server.py`
2. Open `http://localhost:5000/admin.html`
3. Go to **Pipeline Configuration → Scene Generation → Local — Wan2.1 / LTX-Video**
4. Click the purple **🔧 Setup & Start Local Wan2.1 / LTX-Video** button
5. The system auto-installs ComfyUI, the Python venv, PyTorch CUDA, and the Wan2.1 custom node — then launches ComfyUI
6. Click "Test Local" to verify ComfyUI is running and exposing Wan/LTX nodes

**Option B — PowerShell script (manual, one-time):**
```powershell
.\scripts\setup_comfyui_wan.ps1
```
Requires: NVIDIA GPU + CUDA, Python 3.10–3.12, Git.

**Requirements (installed by either method into `comfyui/.venv`):**
- Python 3.10–3.12 virtual environment
- PyTorch CUDA (`torch`, `torchvision`, `torchaudio`)
- ComfyUI + a Wan2.1 / LTX-Video custom node pack (e.g. `ComfyUI-WanVideoWrapper`)
- A Wan2.1 or LTX-Video checkpoint downloaded into `comfyui/models/Diffusion_Models/`

> **Scene providers are fully configurable.** Open **Admin → Scene Generation**, pick **Local — Wan2.1 / LTX-Video**, **fal.ai**, or **Replicate**, set the model/endpoint/timeout and API key, then click **Save & Activate**. The active provider and its settings are stored in the database — no redeploy needed to switch.

---

## Environment Configuration

Create `.env` from `.env.example`. All keys can also be set live from the **Admin Portal → Settings**.

```env
# ── Auth / Security ──────────────────────────────────────────────────────────
JWT_SECRET_KEY=change-me-to-a-strong-random-secret-at-least-32-chars
# Generate: python -c "import secrets; print(secrets.token_urlsafe(32))"
JWT_ALGORITHM=HS256
JWT_EXPIRE_MIN=10080
AUTH_REQUIRED=false
CORS_ORIGINS=*

# ── Transcription Provider ──────────────────────────────────────────────────
# Options: groq (free whisper-large-v3) | assemblyai (cloud paid) | faster_whisper (local, no key)
TRANSCRIPTION_PROVIDER=groq
GROQ_API_KEY=your_groq_key_here
ASSEMBLYAI_API_KEY=

# ── AI Ranking / LLM Provider ───────────────────────────────────────────────
# Options: gemini | openai | groq | ollama | deepseek | openrouter | custom (OpenAI-compatible)
RANKING_PROVIDER=groq
# Gemini
GOOGLE_API_KEY=
# OpenAI / Groq / OpenRouter
OPENAI_API_KEY=your_openai_key_here
# Custom OpenAI-compatible endpoint (Groq, DeepSeek, vLLM, LM Studio, etc.)
CUSTOM_AI_BASE_URL=https://api.groq.com/openai/v1
CUSTOM_AI_API_KEY=your_groq_key_here
CUSTOM_AI_MODEL=llama-3.1-8b-instant
# Ollama (local LLM — must be running: ollama serve)
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=qwen2.5:3b

# ── Local ComfyUI (optional) ────────────────────────────────────────────────
COMFYUI_URL=http://127.0.0.1:8188

# ── Server ─────────────────────────────────────────────────────────────────
HOST=0.0.0.0
PORT=5000
RELOAD=false

# ── Video Downloader ─────────────────────────────────────────────────────────
VIDEOSAILOR_API_KEY=
```

**Minimum to start (100% free):**
- `TRANSCRIPTION_PROVIDER=groq` + `GROQ_API_KEY` → free Whisper transcription
- `RANKING_PROVIDER=groq` + `OPENAI_API_KEY` (with Groq key) → free Llama ranking
- `JWT_SECRET_KEY` → any strong random string

`AUTH_REQUIRED=false` lets guests use the app without login. Default admin: `admin` / `Admin@123`.

---

## Running the Server

```bash
# Production
python server.py --host 0.0.0.0 --port 5000

# Development (auto-reload)
python server.py --reload
```

**URLs:**
- App: `http://localhost:5000/`
- Admin: `http://localhost:5000/admin.html`
- Swagger: `http://localhost:5000/docs`
- Health: `http://localhost:5000/api/health`

---

## Default Credentials

| Role | Username | Password |
|---|---|---|
| **System Owner** | `admin` | `Admin@123` |

Standard users self-register at `/signup.html`. Owner has unlimited quota.

---

## Web UI & Admin Portal

### Main App (`/`)

- Paste a YouTube URL → auto-download, transcribe, rank clips, render shorts
- Paste a topic/idea → generate a viral script, then render as AI-generated scenes

### Admin Portal (`/admin.html`)

| Tab | Description |
|---|---|
| **Overview** | System status, active jobs, audit log stream |
| **API Settings** | All provider keys, ranking/transcription/speech provider selection |
| **Pipeline Config** | Video output spec, clip selection, semantic ranking, captions, prompts |
| **Scene Generation** | Local Wan2.1 / LTX-Video, fal.ai, or Replicate + Setup & Activate |
| **Prompt Templates** | Edit every AI step's system prompt; Live Test with actual LLM call |
| **Jobs** | Monitor all generation jobs, view rendered output, batch delete |
| **Users** | List users, toggle active, set quota, delete |

---

## AI Providers & Local Video Generation

### Scene/Clip Video Generation (3 options)

| Provider | Cost | Notes |
|---|---|---|
| **Local — Wan2.1 / LTX-Video** | Free / offline | Your GPU via ComfyUI. Model + endpoint configurable. |
| **fal.ai** | Pay-as-you-go | Cloud. Configurable model endpoint (e.g. Kling). |
| **Replicate** | Pay-as-you-go | Cloud. Default `wan-video/wan-2.1-t2v-14b`. |

Exactly one provider is **active** at a time. Open **Admin → Pipeline Config → Scene Generation**, configure and click **Save & Activate**. Switching providers takes effect immediately — no redeploy.

### AI Ranking / LLM (Script Generation + Clip Scoring)

| Provider | Cost | Notes |
|---|---|---|
| **Groq** (Llama / Qwen) | Free tier | Fastest inference. Recommended. |
| **Gemini** (Google) | Free tier | `GOOGLE_API_KEY` |
| **OpenAI** | Paid | `OPENAI_API_KEY` |
| **Ollama** (local) | Free | Any GGUF model. Run `ollama serve`. |
| **Custom OpenAI-compatible** | — | Groq, DeepSeek, vLLM, LM Studio, etc. |

Set `RANKING_PROVIDER` in `.env` or Admin → API Settings. The Prompt Live Test in Admin Portal shows exactly which provider + model is being used and fails gracefully if the provider isn't configured.

---

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/api/health` | GET | System health check |
| `/api/auth/login` | POST | Login (users & admin) |
| `/api/auth/signup` | POST | Self-service registration |
| `/api/auth/me` | GET | Current session details |
| `/api/pipeline/auto-generate` | POST | 1-Click YouTube → 9:16 shorts |
| `/api/pipeline/generate-from-topic` | POST | AI topic → short script → rendered scenes |
| `/api/pipeline/script-to-video` | POST | Script + TTS → AI scene video |
| `/api/admin/config` | GET/POST | Provider keys & config |
| `/api/admin/pipeline-config` | GET/POST | Full pipeline settings |
| `/api/admin/prompts` | GET/POST | List/create prompt templates |
| `/api/admin/prompts/{id}` | GET/PUT | Get/update a single prompt |
| `/api/admin/prompts/{id}/test?live=true` | POST | Live-test a prompt (calls actual LLM) |
| `/api/admin/prompts/{id}/activate` | POST | Activate a prompt version |
| `/api/admin/scene-providers` | GET/POST | List / save scene providers |
| `/api/admin/scene-providers/{key}/activate` | POST | Set active scene provider |
| `/api/admin/scene-providers/{key}/clear-key` | POST | Clear a scene provider's API key |
| `/api/admin/comfyui/status` | GET | Check Wan2.1 / LTX-Video ComfyUI install status |
| `/api/admin/comfyui/setup` | POST | Auto-install ComfyUI + models |
| `/api/admin/comfyui/start` | POST | Launch ComfyUI background process |
| `/api/admin/video-provider/test` | POST | Test video provider (cloud or local) |
| `/api/admin/system-health` | GET | CPU/RAM/disk metrics |
| `/api/admin/audit` | GET | Audit log stream & batch delete |
| `/api/admin/jobs` | GET | List all jobs with pagination |
| `/api/admin/trials/reset` | POST | Purge guest device tracking |

Full interactive docs at `/docs`.

---

## Deployment

| Platform | File | Command |
|---|---|---|
| **Docker** | `Dockerfile` | `docker build -t vergeclip . && docker run -p 5000:5000 vergeclip` |
| **Docker Compose** | `docker-compose.yml` | `docker-compose up -d` |
| **Render** | `render.yaml` | Blueprint (Docker, port 5000, `/health`) |
| **Railway / Nixpacks** | `nixpacks.toml` | `pip install -r requirements.txt`, run `server.py` |
| **Heroku / Fly.io** | `Procfile` | `web: python server.py --host 0.0.0.0 --port $PORT` |

> **Local Wan2.1 / LTX-Video (ComfyUI)** is **not** part of Docker/web deployments — it requires a GPU. Use **fal.ai** or **Replicate** in containers.

Set all API keys via environment variables (platform secrets) or the Admin Portal. `JWT_SECRET_KEY` is the only truly required secret; everything else has safe defaults.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError` on fresh pull | `pip install -r requirements.txt` in activated venv |
| `faster-whisper` not found | Run `pip install faster-whisper` |
| FFmpeg not found | Install FFmpeg on PATH, or rely on bundled `imageio-ffmpeg` fallback |
| Black bars / letterboxed shorts | Set exact Width/Height in **Admin → Pipeline → Video Output Spec** |
| Prompt Live Test gives 422 error | The prompt test endpoint was fixed in the latest version — pull latest code and restart |
| Prompt Live Test gives 503 error | LLM provider not configured. Set `RANKING_PROVIDER` + its key in **Admin → API Settings** |
| Live Test says "not configured" for gemini | `GOOGLE_API_KEY` is not set. Either add it or switch `RANKING_PROVIDER` to `groq`/`ollama` |
| Local says "no Wan/LTX nodes found" | ComfyUI is running but lacks a Wan2.1/LTX-Video custom node pack + checkpoint. Install `ComfyUI-WanVideoWrapper` and load a Wan/LTX checkpoint, or switch to a cloud provider. |
| ComfyUI won't start | Check `data/comfyui.log` for errors; ensure NVIDIA GPU + CUDA driver installed |
| OOM / CUDA out of memory | Wan/LTX is memory-hungry — the local provider auto-reduces render resolution. Lower latency/preview resolution in the app or use a cloud provider. |
| Auth errors | Set `JWT_SECRET_KEY`; use `AUTH_REQUIRED=false` for guest access |

---

© 2026 **Vergeclip AI**. Built with FastAPI, Whisper, OpenCV, FFmpeg, edge-tts, Wan2.1 / LTX-Video & ComfyUI.
