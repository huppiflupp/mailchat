"""Update mailchat from its GitHub repository, on request only.

A git working copy is updated with `git pull --ff-only`, an installed package
(pip or pipx) with pip from the release's source archive. Afterwards the server
replaces itself with the new code (os.execv), so systemd or the autostart entry
keep running the same process. The add-on updates through Thunderbird
(update_url in its manifest).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import __version__

REPO = "huppiflupp/mailchat"
ROOT = Path(__file__).resolve().parent.parent
_lock = threading.Lock()


def _version(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in text.lstrip("vV").split("."))
    except ValueError:
        return ()


def method() -> str:
    return "git" if (ROOT / ".git").exists() else "pip"


def check() -> dict:
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                 headers={"Accept": "application/vnd.github+json",
                                          "User-Agent": f"mailchat/{__version__}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        rel = json.loads(r.read())
    tag = rel.get("tag_name", "")
    xpi = next((a["browser_download_url"] for a in rel.get("assets", [])
                if a.get("name", "").endswith(".xpi")), "")
    return {"current": __version__, "latest": tag.lstrip("vV"), "tag": tag,
            "available": _version(tag) > _version(__version__),
            "notes": (rel.get("body") or "")[:2000], "url": rel.get("html_url", ""),
            "addon_url": xpi, "method": method()}


def install(tag: str) -> dict:
    """Fetch the new code. Returns {"ok": True, "output": …} or {"error": …}."""
    if not _lock.acquire(blocking=False):
        return {"error": "update already running"}
    try:
        if method() == "git":
            dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain",
                                    "--untracked-files=no"], capture_output=True, text=True)
            if dirty.stdout.strip():
                return {"error": "local changes in " + str(ROOT) + ":\n" + dirty.stdout}
            cmd = ["git", "-C", str(ROOT), "pull", "--ff-only"]
        else:
            cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet",
                   f"https://github.com/{REPO}/archive/refs/tags/{tag}.zip"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        out = (res.stdout + res.stderr).strip()[-2000:]
        if res.returncode != 0:
            return {"error": out or f"{cmd[0]} failed ({res.returncode})"}
        return {"ok": True, "output": out}
    finally:
        _lock.release()


def restart_later(busy=lambda: False, delay: float = 1.0) -> None:
    """Replace this process with a fresh one once the HTTP answer is out and no
    index run is in progress (its worker processes would be orphaned)."""
    def run() -> None:
        time.sleep(delay)
        while busy():
            time.sleep(1)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, sys.orig_argv)

    threading.Thread(target=run, daemon=True).start()
