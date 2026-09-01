# ============================================================================
# Vergeclip - Local CogVideoX-2B via ComfyUI (offline video generation)
# ----------------------------------------------------------------------------
# ONE-SHOT A->Z setup for local, GPU-based AI scene/clip generation so that:
#   "koi bhi pull le -> setup chalao -> local video generation ready"
#
# - Installs ComfyUI (its OWN venv under comfyui/, separate from the app)
# - Installs PyTorch CUDA 12.6 + the CogVideoXWrapper deps from the manifest
#     => requirements-local.txt   (single source of truth for local deps)
# - Installs the kijai ComfyUI-CogVideoXWrapper custom node
# - Optionally downloads the CogVideoX-2B + T5 fp8 model files
#
# Requirements:
#   - NVIDIA GPU (>=4GB VRAM; RTX 3050 6GB tested)
#   - NVIDIA driver with CUDA 12.6 support
#   - Python 3.10 - 3.12  (ComfyUI does NOT support 3.13)
#   - Git  (https://git-scm.com)
#   - Windows 10/11 + Winget (for auto-installing Python 3.12 if needed)
#
# Run:
#   .\scripts\setup_comfyui_cogvideo.ps1
# Common flags:
#   -SkipPythonInstall   # you already have Python 3.10-3.12
#   -SkipComfyInstall    # ComfyUI already cloned/installed
#   -SkipModels          # don't re-download model files
# ============================================================================

param(
  [switch]$SkipPythonInstall,
  [switch]$SkipComfyInstall,
  [switch]$SkipModels
)

$ErrorActionPreference = "Stop"
$ProjRoot      = Split-Path -Parent $PSScriptRoot
$ComfyDir      = Join-Path $ProjRoot "comfyui"
$LocalReqs     = Join-Path $ProjRoot "requirements-local.txt"
$python312     = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"

Write-Host "=== Vergeclip local CogVideoX-2B setup (A->Z) ===" -ForegroundColor Cyan
Write-Host "Project root : $ProjRoot"
Write-Host "ComfyUI target: $ComfyDir`n"

# ── 0. Ensure a compatible Python (3.10-3.12) exists ─────────────────────────
function Test-PyCompatible {
  param([string]$cmd)
  try {
    if (-not (Test-Path $cmd)) { return $false }
    $v = (& $cmd --version 2>$null | Select-Object -First 1)
    if ($v -match "Python\s*(3\.1[0-2])") { return $true }
  } catch {}
  return $false
}

if (-not (Test-PyCompatible $python312) -and -not $SkipPythonInstall) {
  Write-Host "[0/6] Installing Python 3.12 (required by ComfyUI; 3.13 won't work)..." -ForegroundColor Yellow
  winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
}
if (-not (Test-PyCompatible $python312)) {
  Write-Host "FATAL: No Python 3.10-3.12 found. Install Python 3.12 then re-run." -ForegroundColor Red
  Write-Host "  Download: https://www.python.org/downloads/release/python-31210/" -ForegroundColor Yellow
  exit 1
}
$python = $python312
Write-Host "Using Python: $python" -ForegroundColor Green

# ── 1. Clone ComfyUI ─────────────────────────────────────────────────────────
if (-not (Test-Path $ComfyDir) -and -not $SkipComfyInstall) {
  Write-Host "[1/6] Cloning ComfyUI..." -ForegroundColor Yellow
  git clone https://github.com/comfyanonymous/ComfyUI.git $ComfyDir
} else {
  Write-Host "[1/6] ComfyUI directory exists, skipping clone." -ForegroundColor Green
}

Push-Location $ComfyDir

# ── 2. Create venv (fresh, using 3.12) ───────────────────────────────────────
Write-Host "[2/6] Creating virtual environment (3.12)..." -ForegroundColor Yellow
if (-not (Test-Path ".venv")) { & $python -m venv .venv }
$python = Join-Path $PWD ".venv\Scripts\python.exe"

# ── 3. Install PyTorch CUDA + local CogVideoX deps from manifest ─────────────
Write-Host "[3/6] Installing PyTorch (CUDA 12.6) + CogVideoX deps from requirements-local.txt..." -ForegroundColor Yellow
& $python -m pip install --upgrade pip
if (-not (Test-Path $LocalReqs)) {
  Write-Host "FATAL: $LocalReqs not found. Keep it next to requirements.txt." -ForegroundColor Red
  exit 1
}
& $python -m pip install -r $LocalReqs

# ── 4. Install ComfyUI's own requirements ────────────────────────────────────
Write-Host "[4/6] Installing ComfyUI requirements..." -ForegroundColor Yellow
& $python -m pip install -r (Join-Path $PWD "requirements.txt")
& $python -m pip install accelerate

# ── 5. Install the kijai CogVideoXWrapper custom node + its deps ─────────────
Write-Host "[5/6] Installing ComfyUI-CogVideoXWrapper custom node..." -ForegroundColor Yellow
if (-not (Test-Path "custom_nodes\ComfyUI-CogVideoXWrapper")) {
  git clone https://github.com/kijai/ComfyUI-CogVideoXWrapper.git "custom_nodes\ComfyUI-CogVideoXWrapper"
}
if (Test-Path "custom_nodes\ComfyUI-CogVideoXWrapper\requirements.txt") {
  & $python -m pip install -r "custom_nodes\ComfyUI-CogVideoXWrapper\requirements.txt"
}
# Also install ComfyUI-Manager (helps keep nodes + models updated in the UI)
if (-not (Test-Path "custom_nodes\ComfyUI-Manager")) {
  Write-Host "   Installing ComfyUI-Manager..." -ForegroundColor DarkGray
  git clone https://github.com/ltdrdata/ComfyUI-Manager.git "custom_nodes\ComfyUI-Manager"
  if (Test-Path "custom_nodes\ComfyUI-Manager\requirements.txt") {
    & $python -m pip install -r "custom_nodes\ComfyUI-Manager\requirements.txt"
  }
}

# ── 6. Download model files (CogVideoX-2B + T5 fp8) ──────────────────────────
if (-not $SkipModels) {
  Write-Host "[6/6] Downloading model files (CogVideoX-2B + T5 fp8)..." -ForegroundColor Yellow
  $modelRoot = Join-Path $PWD "models"
  $cogDir  = Join-Path $modelRoot "CogVideo\CogVideo2B"
  $teDir   = Join-Path $modelRoot "text_encoders"
  New-Item -ItemType Directory -Force -Path "$cogDir\transformer","$cogDir\vae","$cogDir\scheduler","$teDir" | Out-Null

  function Get-HF([string]$url, [string]$dest) {
    if (Test-Path $dest) { Write-Host "   already: $dest" -ForegroundColor DarkGray; return }
    Write-Host "   downloading: $dest" -ForegroundColor Gray
    try {
      Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
    } catch {
      Write-Host "   WARN: failed to download $dest -> $($_.Exception.Message)" -ForegroundColor Yellow
    }
  }

  # THUDM/CogVideoX-2b (HF diffusers format) — matches comfyui/models/CogVideo/CogVideo2B
  $hf = "https://huggingface.co/THUDM/CogVideoX-2b/resolve/main"
  Get-HF "$hf/transformer/diffusion_pytorch_model.safetensors" "$cogDir\transformer\diffusion_pytorch_model.safetensors"
  Get-HF "$hf/transformer/config.json"                          "$cogDir\transformer\config.json"
  Get-HF "$hf/vae/diffusion_pytorch_model.safetensors"          "$cogDir\vae\diffusion_pytorch_model.safetensors"
  Get-HF "$hf/vae/config.json"                                  "$cogDir\vae\config.json"
  Get-HF "$hf/scheduler/scheduler_config.json"                  "$cogDir\scheduler\scheduler_config.json"
  # T5 fp8 text encoder (state-of-the-art for CogVideoX via the wrapper)
  Get-HF "https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/main/t5xxl_fp8_e4m3fn.safetensors" "$teDir\t5xxl_fp8_e4m3fn.safetensors"
} else {
  Write-Host "[6/6] Skipped model download (-SkipModels)." -ForegroundColor DarkGray
}

Pop-Location

# ── Final instructions ────────────────────────────────────────────────────────
Write-Host "`n=== DONE - HOW TO USE LOCAL VIDEO ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "STEP 1 - Start ComfyUI (keep this window running):" -ForegroundColor Green
Write-Host "   $ComfyDir\run_nvidia_gpu.bat"
Write-Host "   (or: $ComfyDir\.venv\Scripts\python.exe main.py --listen 127.0.0.1 --port 8188)"
Write-Host ""
Write-Host "STEP 2 - Open: http://127.0.0.1:8188  (confirm CogVideoXWrapper node loads)" -ForegroundColor Green
Write-Host ""
Write-Host "STEP 3 - In Vergeclip Admin:" -ForegroundColor Green
Write-Host "   Admin -> Pipeline Configuration -> Scene Generation -> provider = 'Local CogVideoX-2B (GPU)'"
Write-Host "   ComfyUI URL = http://127.0.0.1:8188  -> click '[X] Check ComfyUI / GPU'"
Write-Host ""
Write-Host "TIP - Keep a saved API workflow:" -ForegroundColor DarkGray
Write-Host "   Export a working CogVideo workflow as 'comfyui_workflow_api.json' in the project root;"
Write-Host "   Vergeclip drives it directly from your prompt ({prompt} / width / height / seed)."
