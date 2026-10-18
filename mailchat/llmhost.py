"""Check that the LLM answers and, optionally, start it on demand.

By default mailchat only checks the endpoint: Ollama loads models by itself.
With LLM_UNIT set (Linux, systemd user unit), mailchat starts that unit when
nothing answers. Ollama's models are unloaded first, since both share the GPU,
and a unit that mailchat started is stopped again after LLM_IDLE seconds
without a request.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.request
from collections.abc import Iterator

from . import config
from .i18n import t

_lock = threading.Lock()
_state = {"started_by_us": False, "last_use": 0.0, "watcher": False}


def _base() -> str:
    return config.LLM_URL.split("/v1/")[0]


def ok() -> bool:
    """/v1/models is served by Ollama, llama-server, LM Studio and vLLM alike.
    llama-server answers 503 there while the model is still loading."""
    req = urllib.request.Request(_base() + "/v1/models")
    if config.LLM_API_KEY:
        req.add_header("Authorization", f"Bearer {config.LLM_API_KEY}")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args, config.LLM_UNIT],
                          capture_output=True, text=True, timeout=60)


def _active() -> bool:
    return _systemctl("is-active", "--quiet").returncode == 0


def _journal(since: str) -> str:
    return subprocess.run(["journalctl", "--user", "-u", config.LLM_UNIT, "--since", since,
                           "--no-pager", "-o", "cat"],
                          capture_output=True, text=True, timeout=10).stdout


def _unload_ollama() -> list[str]:
    """Ask Ollama to drop every model it holds in VRAM; returns their names."""
    base = config.OLLAMA_URL
    if base.rstrip("/") == _base().rstrip("/"):
        return []  # the LLM is Ollama itself
    try:
        with urllib.request.urlopen(f"{base}/api/ps", timeout=5) as r:
            models = json.loads(r.read()).get("models") or []
    except Exception:
        return []  # Ollama not running: nothing to free
    names = []
    for m in models:
        if not m.get("size_vram"):
            continue
        body = json.dumps({"model": m["name"], "keep_alive": 0}).encode()
        req = urllib.request.Request(f"{base}/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=60).read()
            names.append(m["name"])
        except Exception:
            pass
    return names


def _gpu_users() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=process_name,used_memory",
             "--format=csv,noheader"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""
    return "; ".join(line.strip() for line in out.splitlines() if line.strip())


def _why_failed(lang: str, since: str) -> str:
    log = _journal(since)
    if "out of memory" in log:
        users = _gpu_users()
        return t(lang, "gpu_full") + (t(lang, "gpu_users", users=users) if users else "")
    errors = [l for l in log.splitlines() if " E " in l]
    return errors[-1].strip() if errors else "journalctl --user -u " + config.LLM_UNIT


def ensure(lang: str = "en") -> Iterator[str]:
    """Make sure the LLM answers, yielding status texts while it starts.

    Raises RuntimeError with a readable reason."""
    _state["last_use"] = time.time()
    if ok():
        return
    if not config.LLM_UNIT:
        raise RuntimeError(t(lang, "llm_unreachable", url=_base(), model=config.LLM_MODEL))
    with _lock:
        if ok():
            return
        since = time.strftime("%Y-%m-%d %H:%M:%S")
        if not _active():
            freed = _unload_ollama()
            yield t(lang, "llm_starting",
                    freed=t(lang, "llm_freed", names=", ".join(freed)) if freed else "")
            res = _systemctl("start")
            if res.returncode != 0:
                raise RuntimeError(t(lang, "llm_unit_failed", unit=config.LLM_UNIT,
                                     err=res.stderr.strip()))
            _state["started_by_us"] = True
            _watch()
        else:
            yield t(lang, "llm_loading")
        deadline = time.time() + config.LLM_START_TIMEOUT
        while time.time() < deadline:
            if ok():
                _state["last_use"] = time.time()
                return
            # Restart=on-failure would retry forever; one failed load is enough to report.
            if "Failed with result" in _journal(since):
                reason = _why_failed(lang, since)
                _systemctl("stop")
                raise RuntimeError(t(lang, "llm_load_failed", reason=reason))
            time.sleep(2)
        reason = _why_failed(lang, since)
        _systemctl("stop")
        raise RuntimeError(t(lang, "llm_timeout", seconds=config.LLM_START_TIMEOUT, reason=reason))


def touch() -> None:
    _state["last_use"] = time.time()


def _watch() -> None:
    if _state["watcher"] or config.LLM_IDLE <= 0:
        return
    _state["watcher"] = True

    def loop() -> None:
        while True:
            time.sleep(60)
            if (_state["started_by_us"] and time.time() - _state["last_use"] > config.LLM_IDLE
                    and _lock.acquire(blocking=False)):
                try:
                    if _active():
                        print(f"{config.LLM_UNIT}: idle for {config.LLM_IDLE} s, "
                              "stopping it to free the GPU", flush=True)
                        _systemctl("stop")
                    _state["started_by_us"] = False
                finally:
                    _lock.release()

    threading.Thread(target=loop, daemon=True).start()
