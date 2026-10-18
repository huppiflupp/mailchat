"""Voice input: record on this machine, transcribe with a whisper.cpp server.

Recording happens in the server process rather than in the Thunderbird tab,
because extension pages in Thunderbird have no dependable microphone access.
Linux records with pw-record (PipeWire); Windows and macOS (and Linux without
PipeWire) with the optional Python package sounddevice.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
import wave

from . import config
from .i18n import t

MAX_SECONDS = 60  # safety net if the stop click never arrives

_lock = threading.Lock()
_rec: dict = {}  # {"proc", "path", "started", "timer"}


def whisper_ok() -> bool:
    try:
        with urllib.request.urlopen(config.WHISPER_URL.rsplit("/", 1)[0] + "/", timeout=2):
            return True
    except Exception:
        return False


def recording() -> bool:
    return bool(_rec)


class _PwRecord:
    def __init__(self, path: str):
        cmd = ["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16"]
        if config.MIC:
            cmd += ["--target", config.MIC]
        self.proc = subprocess.Popen(cmd + [path], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE)

    def stop(self) -> None:
        self.proc.send_signal(signal.SIGINT)  # SIGINT makes pw-record write the WAV header
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class _SoundDevice:
    def __init__(self, path: str):
        import sounddevice as sd  # optional dependency: pip install sounddevice
        self.wav = wave.open(path, "wb")
        self.wav.setnchannels(1)
        self.wav.setsampwidth(2)
        self.wav.setframerate(16000)
        device = int(config.MIC) if config.MIC.isdigit() else (config.MIC or None)
        self.stream = sd.RawInputStream(samplerate=16000, channels=1, dtype="int16",
                                        device=device,
                                        callback=lambda data, *_: self.wav.writeframes(bytes(data)))
        self.stream.start()

    def stop(self) -> None:
        self.stream.stop()
        self.stream.close()
        self.wav.close()


def _recorder(path: str):
    if not config.WINDOWS and shutil.which("pw-record"):
        return _PwRecord(path)
    return _SoundDevice(path)


def available() -> bool:
    if not config.WINDOWS and shutil.which("pw-record"):
        return True
    try:
        import sounddevice  # noqa: F401
        return True
    except Exception:
        return False


def start(lang: str = "en") -> dict:
    with _lock:
        if _rec:
            return {"status": t(lang, "voice_running")}
        fd, path = tempfile.mkstemp(prefix="mailchat-", suffix=".wav")
        os.close(fd)
        try:
            rec = _recorder(path)
        except Exception as e:
            os.unlink(path)
            return {"error": t(lang, "voice_no_recorder", err=e)}
        timer = threading.Timer(MAX_SECONDS, cancel)
        timer.daemon = True
        timer.start()
        _rec.update(rec=rec, path=path, started=time.time(), timer=timer)
    return {"status": t(lang, "voice_recording")}


def _finish() -> tuple[str | None, float]:
    """Stop the recorder and return the WAV file."""
    with _lock:
        if not _rec:
            return None, 0.0
        rec = dict(_rec)
        _rec.clear()
    rec["timer"].cancel()
    try:
        rec["rec"].stop()
    except Exception:
        pass
    return rec["path"], time.time() - rec["started"]


def cancel(lang: str = "en") -> dict:
    path, _ = _finish()
    if path:
        os.unlink(path)
    return {"status": t(lang, "voice_cancelled")}


def _transcribe(path: str, lang: str) -> str:
    boundary = uuid.uuid4().hex
    with open(path, "rb") as fh:
        audio = fh.read()
    parts = []
    for name, value in (("response_format", "json"), ("temperature", "0"),
                        ("language", lang)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                     f"\r\n\r\n{value}\r\n".encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                 f'filename="voice.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
                 + audio + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        config.WHISPER_URL, data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read()).get("text", "")


# Whisper's well-known hallucinations on silence / noise.
_NOISE = {"", "untertitel der amara.org-community", "untertitelung des zdf, 2020",
          "vielen dank.", "danke.", "tschüss.", "musik", "[musik]", "*musik*",
          "thank you.", "thanks for watching!", "you", "[music]", "[blank_audio]"}


def stop(lang: str = "en") -> dict:
    path, seconds = _finish()
    if not path:
        return {"error": t(lang, "voice_none")}
    try:
        if seconds < 0.6 or os.path.getsize(path) < 16000:
            return {"text": "", "seconds": round(seconds, 1)}
        text = " ".join(_transcribe(path, lang).split())
    except Exception as e:
        return {"error": t(lang, "voice_failed", err=e)}
    finally:
        os.unlink(path)
    if text.lower().strip(" .!") in {n.strip(" .!") for n in _NOISE}:
        text = ""
    return {"text": text, "seconds": round(seconds, 1)}
