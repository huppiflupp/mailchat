"""Paths, endpoints and settings.

Every setting can be given in three places, the first one wins:
1. environment variable MAILCHAT_<NAME>, e.g. MAILCHAT_LLM_MODEL
2. config file (see CONFIG_FILE), key <name> in lower case, e.g. llm_model = "gemma4:12b"
3. the default below
"""
from __future__ import annotations

import configparser
import os
import sys
import tomllib
from pathlib import Path

HOME = Path.home()
WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"

if WINDOWS:
    _CONFIG_DIR = Path(os.environ.get("APPDATA", HOME / "AppData/Roaming")) / "mailchat"
    _DATA_DIR = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData/Local")) / "mailchat"
elif MACOS:
    _CONFIG_DIR = _DATA_DIR = HOME / "Library/Application Support/mailchat"
else:
    _CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "mailchat"
    _DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "mailchat"

CONFIG_FILE = Path(os.environ.get("MAILCHAT_CONFIG", _CONFIG_DIR / "config.toml"))
try:
    _FILE = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))
except FileNotFoundError:
    _FILE = {}


def _get(name: str, default):
    raw = os.environ.get("MAILCHAT_" + name.upper())
    if raw is None:
        raw = _FILE.get(name, default)
    return type(default)(raw) if default is not None else raw


# ------------------------------------------------------------------ Thunderbird profile

def thunderbird_dirs() -> list[Path]:
    """Places where Thunderbird keeps profiles.ini, most common first."""
    if WINDOWS:
        return [Path(os.environ.get("APPDATA", HOME / "AppData/Roaming")) / "Thunderbird"]
    if MACOS:
        return [HOME / "Library/Thunderbird"]
    return [HOME / ".thunderbird",
            HOME / ".var/app/org.mozilla.Thunderbird/.thunderbird",  # Flatpak
            HOME / "snap/thunderbird/common/.thunderbird"]          # Snap


def find_profile() -> Path | None:
    """The profile Thunderbird starts with: the [Install…] default, else Default=1."""
    for base in thunderbird_dirs():
        ini = base / "profiles.ini"
        if not ini.is_file():
            continue
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(ini, encoding="utf-8")
        profiles = {s: cp[s] for s in cp.sections() if s.startswith("Profile")}

        def resolve(path: str, relative: bool = True) -> Path:
            return base / path if relative else Path(path)

        for s in cp.sections():
            if s.startswith("Install") and cp[s].get("Default"):
                path = cp[s]["Default"]
                rel = next((p.get("IsRelative", "1") == "1" for p in profiles.values()
                            if p.get("Path") == path), True)
                return resolve(path, rel)
        ordered = sorted(profiles.values(), key=lambda p: p.get("Default") != "1")
        for p in ordered:
            if p.get("Path"):
                return resolve(p["Path"], p.get("IsRelative", "1") == "1")
    return None


_profile = _get("profile", "")
PROFILE = Path(_profile).expanduser() if _profile else find_profile()
# Every mbox below these directories is indexed (all IMAP accounts + Local Folders).
MAIL_ROOTS = [PROFILE / "ImapMail", PROFILE / "Mail"] if PROFILE else []
# Folder names (last path component, case-insensitive) that are never indexed.
SKIP_FOLDERS = {s.strip().lower() for s in _get("skip_folders", "spam,junk").split(",") if s.strip()}

DATA_DIR = Path(_get("data", str(_DATA_DIR))).expanduser()
DB_PATH = DATA_DIR / "index.db"

# ------------------------------------------------------------------ LLM

# Any OpenAI-compatible chat endpoint with tool calling: Ollama (default),
# llama.cpp's llama-server, LM Studio, vLLM …
LLM_URL = _get("llm_url", "http://127.0.0.1:11434/v1/chat/completions")
LLM_MODEL = _get("llm_model", "gemma4:12b")
LLM_API_KEY = _get("llm_api_key", "")
# Characters of conversation sent to the model before it must answer (~3 chars/token).
# 60 000 suits a 32k context; lower it for smaller contexts.
LLM_CONTEXT_CHARS = _get("llm_context_chars", 60_000)

# Optional, Linux only: a systemd user unit that serves LLM_URL and is started on
# demand. Ollama's models are unloaded first, since both share the GPU. A unit
# started by mailchat is stopped again after LLM_IDLE seconds without a chat
# (0 = keep it running). Empty LLM_UNIT = never start anything.
LLM_UNIT = _get("llm_unit", "")
LLM_START_TIMEOUT = _get("llm_start_timeout", 180)
LLM_IDLE = _get("llm_idle", 1200)
OLLAMA_URL = _get("ollama_url", "http://127.0.0.1:11434")

# ------------------------------------------------------------------ server

HOST = "127.0.0.1"  # never expose mail content to the network
PORT = _get("port", 8095)
# Seconds between incremental index runs while the server is up.
REINDEX_INTERVAL = _get("reindex", 600)

# Characters of body text kept per message (index + what the LLM may read).
BODY_LIMIT = 40_000

# ------------------------------------------------------------------ voice input (optional)

# whisper.cpp server; voice input is offered only while it answers.
WHISPER_URL = _get("whisper_url", "http://127.0.0.1:8096/inference")
# Recording device: Linux = PipeWire source for pw-record (empty = default),
# Windows/macOS = sounddevice input device name or number (empty = default).
MIC = _get("mic", "")
