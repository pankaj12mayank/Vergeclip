# ============================================================================
# Vergeclip - Local Wan2.1 / LTX-Video via ComfyUI (offline video generation)
# ----------------------------------------------------------------------------
# ONE-SHOT A->Z setup for local, GPU-based AI scene/clip generation so that:
#   "koi bhi pull le -> setup chalao -> local video generation ready"
#
# - Installs ComfyUI (its OWN venv under comfyui/, separate from the app)
# - Installs PyTorch CUDA + WanVideoWrapper deps from the manifest
#     => requirements-local.txt   (single source of truth for local deps)
# - Installs kijai ComfyUI-WanVideoWrapper + ComfyUI-KJNodes custom nodes
# - Optionally downloads Wan2.1-T2V-14B + UMT5-XXL + VAE model files
#
# Requirements:
#   - NVIDIA GPU (>=6GB VRAM recommended for Wan2.1-14B; LTX smaller)
#   - NVIDIA driver with CUDA support
#   - Python 3.10 - 3.12  (ComfyUI does NOT support 3.13)
#   - Git  (https://git-scm.com)
#   - Windows 10/11 + Winget (for auto-installing Python 3.12 if needed)
#
# Run:
#   .\scripts\setup_comfyui_wan.ps1
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

Write-Host "=== Vergeclip local Wan2.1 / LTX-Video setup (A->Z) ===" -ForegroundColor Cyan
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

# ── 3. Install PyTorch CUDA + Wan deps from manifest ─────────────────────────
Write-Host "[3/6] Installing PyTorch (CUDA) + Wan deps from requirements-local.txt..." -ForegroundColor Yellow
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

# ── 5. Install WanVideoWrapper + KJNodes custom nodes + deps ─────────────────
Write-Host "[5/6] Installing ComfyUI-WanVideoWrapper + KJNodes..." -ForegroundColor Yellow
if (-not (Test-Path "custom_nodes\ComfyUI-WanVideoWrapper")) {
  git clone https://github.com/kijai/ComfyUI-WanVideoWrapper.git "custom_nodes\ComfyUI-WanVideoWrapper"
}
if (Test-Path "custom_nodes\ComfyUI-WanVideoWrapper\requirements.txt") {
  & $python -m pip install -r "custom_nodes\ComfyUI-WanVideoWrapper\requirements.txt"
}
if (-not (Test-Path "custom_nodes\ComfyUI-KJNodes")) {
  git clone https://github.com/kijai/ComfyUI-KJNodes.git "custom_nodes\ComfyUI-KJNodes"
}
if (Test-Path "custom_nodes\ComfyUI-KJNodes\requirements.txt") {
  & $python -m pip install -r "custom_nodes\ComfyUI-KJNodes\requirements.txt"
}
# Also install ComfyUI-Manager (helps keep nodes + models updated in the UI)
if (-not (Test-Path "custom_nodes\ComfyUI-Manager")) {
  Write-Host "   Installing ComfyUI-Manager..." -ForegroundColor DarkGray
  git clone https://github.com/ltdrdata/ComfyUI-Manager.git "custom_nodes\ComfyUI-Manager"
  if (Test-Path "custom_nodes\ComfyUI-Manager\requirements.txt") {
    & $python -m pip install -r "custom_nodes\ComfyUI-Manager\requirements.txt"
  }
}

# ── 6. Download model files (Wan2.1-T2V-14B + UMT5-XXL + VAE) ──────────────────
if (-not $SkipModels) {
  Write-Host "[6/6] Downloading model files (Wan2.1-T2V-14B + UMT5-XXL + VAE)..." -ForegroundColor Yellow
  $modelRoot = Join-Path $PWD "models"
  $diffDir = Join-Path $modelRoot "Diffusion_Models"
  $teDir   = Join-Path $modelRoot "text_encoders"
  $vaeDir  = Join-Path $modelRoot "vae"
  New-Item -ItemType Directory -Force -Path $diffDir,$teDir,$vaeDir | Out-Null

  function Get-HF([string]$url, [string]$dest) {
    if (Test-Path $dest) { Write-Host "   already: $dest" -ForegroundColor DarkGray; return }
    Write-Host "   downloading: $dest" -ForegroundColor Gray
    try {
      Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
    } catch {
      Write-Host "   WARN: failed to download $dest -> $($_.Exception.Message)" -ForegroundColor Yellow
    }
  }

  # Comfy-Org Wan 2.1 repackaged for ComfyUI
  $hf = "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files"
  Get-HF "$hf/diffusion_models/wan2.1_t2v_14B_bf16.safetensors"          "$diffDir\wan2.1_t2v_14B_bf16.safetensors"
  Get-HF "$hf/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"     "$teDir\umt5_xxl_fp8_e4m3fn_scaled.safetensors"
  Get-HF "$hf/vae/wan_2.1_vae.safetensors"                              "$vaeDir\wan_2.1_vae.safetensors"
  Write-Host "   LTX-Video alternative: download an LTX checkpoint to models/checkpoints/ and use it via the LTX nodes." -ForegroundColor DarkGray
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
Write-Host "STEP 2 - Open: http://127.0.0.1:8188  (confirm WanVideoWrapper nodes load)" -ForegroundColor Green
Write-Host ""
Write-Host "STEP 3 - In Vergeclip Admin:" -ForegroundColor Green
Write-Host "   Admin -> Pipeline Configuration -> Scene Generation -> provider = 'Local — Wan2.1 / LTX-Video (GPU)'"
Write-Host "   ComfyUI URL = http://127.0.0.1:8188  -> click 'Test Local'"
Write-Host ""
Write-Host "TIP - Keep a saved API workflow:" -ForegroundColor DarkGray
Write-Host "   Export a working Wan/LTX workflow as 'comfyui_workflow_api.json' in the project root;"
Write-Host "   Vergeclip drives it directly from your prompt ({prompt} / width / height / seed)."
