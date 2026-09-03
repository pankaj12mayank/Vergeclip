"""
src/ffmpeg_utils.py
-------------------
Shared hardened FFmpeg runner used across the whole render pipeline.

Why this exists:
  - Windows ffmpeg can hang waiting on stdin -> we force ``-nostdin`` and pipe
    stdin from DEVNULL so the process can never block on console input.
  - On timeout the direct child must be killed along with its whole process
    tree, otherwise a zombie ffmpeg keeps input/output files open and later
    deletes fail with WinError 32 (file in use by another process).
  - ``CREATE_NO_WINDOW`` prevents a console window from popping up for every
    ffmpeg invocation when running behind the web server.

Public API:
    run_ffmpeg(cmd, timeout=600) -> CompletedProcess
    purge_stale_ffmpeg(max_age_s=600) -> int   (number of processes killed)
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import Optional

_ACTIVE: dict[int, float] = {}
_LOCK = threading.Lock()

STALE_FFMPEG_AGE_S = 600.0


def _resolve_exe(exe: str) -> str:
    """Resolve a bare executable name to a full path.

    Windows ``CreateProcess`` does not reliably find extension-less names like
    ``ffmpeg`` via PATH; resolving to an absolute path removes a whole class of
    spurious ``WinError 2`` failures. Paths that already look absolute are
    returned untouched.
    """
    if os.sep in exe or (os.altsep and os.altsep in exe):
        return exe
    import shutil

    found = shutil.which(exe)
    return found or exe


def _kill_tree(pid: int) -> None:
    """Kill a process and every child of it (the whole tree)."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        else:
            os.killpg(pid, signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    time.sleep(0.2)


def run_ffmpeg(
    cmd: list[str],
    timeout: float = 600.0,
) -> subprocess.CompletedProcess:
    """
    Run an ffmpeg command with hang safety and hard task-kill on timeout.

    Returns the CompletedProcess (``stdout``/``stderr`` decoded as text).
    Raises ``subprocess.TimeoutExpired`` if the command exceeds ``timeout``
    seconds — in that case the whole process tree has already been killed.
    """
    if not cmd:
        raise ValueError("Empty ffmpeg command")

    cmd = list(cmd)
    if not any(str(a).lower() == "-nostdin" for a in cmd):
        cmd.insert(1, "-nostdin")

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        cmd,
        executable=_resolve_exe(cmd[0]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
        start_new_session=(os.name != "nt"),
    )
    with _LOCK:
        _ACTIVE[proc.pid] = time.monotonic()
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            stdout, stderr = proc.communicate()
            raise
    finally:
        with _LOCK:
            _ACTIVE.pop(proc.pid, None)

    out_text = stdout.decode("utf-8", errors="replace")
    err_text = stderr.decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(cmd, proc.returncode, out_text, err_text)


def purge_stale_ffmpeg(max_age_s: float = STALE_FFMPEG_AGE_S) -> int:
    """
    Kill any tracked ffmpeg that has been running longer than ``max_age_s``.

    Zombie ffmpeg processes from earlier crashed renders keep their output
    files locked on Windows (WinError 32 on delete). This helper force-kills
    them so files can be cleaned up again.
    """
    now = time.monotonic()
    to_kill: list[int] = []
    with _LOCK:
        for pid, started in list(_ACTIVE.items()):
            if now - started > max_age_s:
                to_kill.append(pid)
    for pid in to_kill:
        _kill_tree(pid)
    return len(to_kill)