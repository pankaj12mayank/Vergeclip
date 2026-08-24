"""
dev.py
------
Zero-friction local development & production runner for Podcast Shorts Generator.

Features:
- Automatically initializes .env from .env.example
- Automatically creates required runtime directories
- Detects missing dependencies and auto-installs them from requirements.txt
- Verifies FFmpeg installation with helpful platform-specific hints
- Starts the web app & REST API server
- Auto-opens browser to the dashboard

Usage:
    python dev.py
    python dev.py --port 5000
    python dev.py --no-browser
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT_DIR = Path(__file__).parent.resolve()


def print_banner():
    banner = r"""
  ___          _               _     ___ _              _       
 | _ \___  __| |__ __ _ ___ _| |_  / __| |_  ___ _ _ _| |_ ___ 
 |  _/ _ \/ _` / _/ _` (_-<_-<  _| \__ \ ' \/ _ \ '_|_   _(_-< 
 |_| \___/\__,_\__\__,_/__/__/\__| |___/_||_\___/_|   |_| /__/ 
                        AI Shorts Studio
    """
    print(banner)


def check_python_version():
    if sys.version_info < (3, 10):
        print(f"[!] Warning: Python 3.11+ is recommended (found {sys.version.split()[0]}).")


def ensure_env_file():
    env_file = ROOT_DIR / ".env"
    example_file = ROOT_DIR / ".env.example"
    if not env_file.exists() and example_file.exists():
        print("[*] Creating .env from .env.example...")
        shutil.copy(example_file, env_file)


def ensure_directories():
    for folder in ("input", "output", "temp", "logs"):
        (ROOT_DIR / folder).mkdir(parents=True, exist_ok=True)


def check_and_install_dependencies():
    """Verify core packages exist; if missing, auto-install from requirements.txt."""
    required_modules = ["dotenv", "colorama", "cv2", "PIL", "numpy", "tqdm", "fastapi", "uvicorn", "jwt", "passlib"]
    missing = []

    for mod in required_modules:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)

    if missing:
        print(f"[*] Missing dependencies detected ({', '.join(missing)}).")
        req_file = ROOT_DIR / "requirements.txt"
        if req_file.exists():
            print("[*] Automatically installing dependencies from requirements.txt...")
            cmd = [sys.executable, "-m", "pip", "install", "-r", str(req_file)]
            try:
                subprocess.check_call(cmd)
                print("[+] Dependencies successfully installed!\n")
            except subprocess.CalledProcessError as exc:
                print(f"[!] Warning: pip install returned error code {exc.returncode}.")
        else:
            print("[!] requirements.txt not found. Skipping auto-install.")


def check_ffmpeg():
    """Verify ffmpeg is accessible."""
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        print("\n" + "=" * 60)
        print(" [!] NOTICE: FFmpeg not detected in PATH.")
        print("     Video rendering & audio processing require FFmpeg.")
        if sys.platform == "win32":
            print("     Windows: Run 'winget install Gyan.FFmpeg' or download from ffmpeg.org")
        elif sys.platform == "darwin":
            print("     macOS: Run 'brew install ffmpeg'")
        else:
            print("     Linux: Run 'sudo apt install ffmpeg'")
        print("=" * 60 + "\n")
    else:
        print(f"[+] FFmpeg found at: {ffmpeg_path}")


def main():
    parser = argparse.ArgumentParser(description="Podcast Shorts Studio Runner")
    parser.add_argument("--port", type=int, default=None, help="Port to listen on (default: 5000 or $PORT)")
    parser.add_argument("--host", type=str, default=None, help="Host to bind to (default: 0.0.0.0 or $HOST)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development (uvicorn --reload)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open the browser")
    args = parser.parse_args()

    print_banner()
    check_python_version()
    ensure_env_file()
    ensure_directories()
    # Ensure data dir for auth DB
    (ROOT_DIR / "data").mkdir(parents=True, exist_ok=True)
    check_and_install_dependencies()
    check_ffmpeg()

    port = args.port or int(os.environ.get("PORT", 5000))
    host = args.host or os.environ.get("HOST", "0.0.0.0")
    # Auto-enable reload in dev when not in production/docker
    reload_flag = args.reload or (os.environ.get("RELOAD", "").lower() in ("1","true","yes")) or (not os.environ.get("PORT") and not os.environ.get("DOCKER"))

    display_url = f"http://localhost:{port}/"

    # Auto-open browser in a background thread
    if not args.no_browser and not os.environ.get("CI") and not os.environ.get("DOCKER"):
        def _open():
            time.sleep(1.2)
            try:
                webbrowser.open(display_url)
            except Exception:
                pass
        import threading
        threading.Thread(target=_open, daemon=True).start()

    print(f"[*] Starting FastAPI + Uvicorn server (reload={reload_flag})...")
    print(f"[*] Docs available at http://localhost:{port}/docs")

    from server import run_server
    run_server(port=port, host=host, reload=reload_flag)


if __name__ == "__main__":
    main()
