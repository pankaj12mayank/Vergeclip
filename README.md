# Vergeclip AI 🎙️⚡ — Viral 9:16 Shorts Generator

**Vergeclip AI** is a state-of-the-art automated AI video pipeline that transforms long-form podcasts, YouTube videos, and raw text topics into high-converting, viral 9:16 short-form clips with intelligent face tracking, kinetic synchronized subtitles, and semantic hook detection.

---

## ✨ Key Features & Architecture

- **🎬 Dual Generation Modes**:
  1. **YouTube URL to Shorts**: Paste any YouTube link to auto-download, transcribe, score viral hooks, center active speakers in 9:16, and burn animated karaoke captions.
  2. **Topic-to-Viral Script Engine (No Video Needed)**: Provide a prompt, idea, or question to instantly generate retention-optimized short scripts with timestamps, hooks, twists, and SEO metadata.
- **🧠 6 Production AI Pipelines (Editable in UI)**:
  - `🧠 Viral Hook & Retention Ranker (v2.0)`
  - `🎯 Face Tracking & Speaker Centering (v2.0)`
  - `💬 Word-by-Word Kinetic Subtitles (v2.0)`
  - `📱 Viral Titles & SEO Hashtags (v2.0)`
  - `💡 Topic-to-Viral Script Pipeline (v2.0)`
  - `🎬 B-Roll & Visual Pacing Roadmap (v2.0)`
- **📝 Real-Time System & User Audit Log Stream**:
  - Live circular buffer & database audit stream with JSON request/response payload inspector.
  - Severity filtering (`SUCCESS`, `ERROR`, `WARN`, `INFO`), batch selection & deletion, and 1-click purge.
- **🤖 Custom AI Provider Support (OpenAI-Compatible)**:
  - Route ranking and script generation through Google Gemini, OpenAI, Groq, DeepSeek, OpenRouter, vLLM, or local Ollama.
- **👑 Single Owner & Client Management**:
  - System Owner account with unlimited quota and zero rate-limiting.
  - Monthly video allowance controls for standard users + 1-click guest device trial reset.
- **🎨 Glassmorphic Cyber Neon Theme & Polish**:
  - Dark Obsidian theme (`#080a11`), Neon Cyan/Purple glowing accents, 3D cinematic motion loaders, and bottom-right human-friendly toast notifications.
  - Symmetrical modals with single top-right `✕` dismiss standard.
  - Strict offline guard that protects client session states when the backend is stopped.

---

## 🚀 Getting Started

### Prerequisites
- **Python 3.11+** (Python 3.12 / 3.13 supported)
- **FFmpeg** installed and accessible on system PATH (`ffmpeg -version`)

### 1. Installation
```bash
# Clone or navigate to the repository
cd Vergeclip

# Install all required dependencies
pip install -r requirements.txt
```

### 2. Environment Configuration
Create a `.env` file in the root directory:
```env
# Core API Keys (Can also be configured live in Admin Portal)
VIDEOSAILOR_API_KEY=your_videosailor_key
ASSEMBLYAI_API_KEY=your_assemblyai_key
GOOGLE_API_KEY=your_gemini_key
OPENAI_API_KEY=your_openai_key

# Security & Session Settings
JWT_SECRET_KEY=your-strong-32+char-secret-key
AUTH_REQUIRED=false
```

### 3. Running the Server

**Development Mode (Auto-Reload):**
```bash
python server.py --reload
# or
uvicorn server:app --reload --port 5000
```

**Production Mode:**
```bash
python server.py --host 0.0.0.0 --port 5000
```

Access the web interface at:
- **Web App**: `http://localhost:5000/`
- **Admin Portal**: `http://localhost:5000/admin.html`
- **Interactive API Docs**: `http://localhost:5000/docs`
- **System Health**: `http://localhost:5000/api/health`

---

## 🔐 Default Credentials

The system initializes with a default System Owner account:
- **Username**: `admin`
- **Password**: `Admin@123`

*Standard users can self-register at `/signup.html` without requiring administrator approval.*

---

## 📡 API Reference Overview

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/api/health` | `GET` | System health check & service status |
| `/api/auth/login` | `POST` | Single login for users and administrators |
| `/api/auth/signup` | `POST` | Self-service client registration |
| `/api/auth/me` | `GET` | Current authenticated session details |
| `/api/pipeline/auto-generate` | `POST` | 1-Click YouTube to 9:16 viral shorts |
| `/api/pipeline/generate-from-topic` | `POST` | AI Topic-to-Short script generator |
| `/api/admin/prompts` | `GET` / `PUT` | AI prompt pipelines management & editor |
| `/api/admin/config/test` | `POST` | 1-Click API key live verification |
| `/api/admin/audit` | `GET` / `DELETE` | Real-time audit log streaming & batch delete |
| `/api/admin/trials/reset` | `POST` | Purge guest device fingerprint tracking |

---

## 🛡️ Security & Privacy Standards
- **Zero Static Leakage**: Strict file handler isolation prevents traversal and hides `.env`, `data/`, and database binaries.
- **Masked Key Inspection**: Secret keys are never returned in plaintext to client browsers.
- **Encrypted Password Storage**: Bcrypt 12-round salted password hashing.

---

© 2026 **Vergeclip AI**. Built with FastAPI, Whisper, OpenCV & FFmpeg.
