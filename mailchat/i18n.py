"""Texts the server shows to the user, in German and English.

The add-on sends Thunderbird's UI language with each chat request; the CLI
uses the system locale. Anything not German falls back to English.
"""
from __future__ import annotations

import locale
import os

TEXTS = {
    "de": {
        "llm_starting": "Starte das Sprachmodell{freed} …",
        "llm_freed": " (Ollama gibt {names} frei)",
        "llm_loading": "Das Sprachmodell lädt noch …",
        "llm_unreachable": "Das Sprachmodell antwortet nicht unter {url}. Läuft Ollama "
                           "(bzw. dein LLM-Server) und ist das Modell „{model}“ installiert?",
        "llm_unit_failed": "{unit} startet nicht: {err}",
        "llm_load_failed": "Das Sprachmodell ließ sich nicht laden: {reason}",
        "llm_timeout": "Das Sprachmodell war nach {seconds} s nicht bereit: {reason}",
        "gpu_full": "GPU-Speicher voll",
        "gpu_users": " (belegt von: {users})",
        "llm_http": "Das Sprachmodell meldet einen Fehler ({status}): {detail}",
        "invented": "\n\n⚠ Die Antwort nennt {ids}, die bei der Suche nicht vorkam{pl} – "
                    "das ist vermutlich erfunden.",
        "invented_pl": "en",
        "no_profile": "Kein Thunderbird-Profil gefunden. Trag den Pfad in {file} ein: "
                      "profile = \"…\"",
        "voice_running": "läuft bereits",
        "voice_recording": "aufnahme",
        "voice_cancelled": "abgebrochen",
        "voice_none": "keine Aufnahme aktiv",
        "voice_failed": "Spracherkennung fehlgeschlagen: {err}",
        "voice_no_recorder": "Keine Aufnahme möglich: {err}",
    },
    "en": {
        "llm_starting": "Starting the language model{freed} …",
        "llm_freed": " (Ollama releases {names})",
        "llm_loading": "The language model is still loading …",
        "llm_unreachable": "The language model does not answer at {url}. Is Ollama "
                           "(or your LLM server) running and is the model \"{model}\" installed?",
        "llm_unit_failed": "{unit} does not start: {err}",
        "llm_load_failed": "The language model could not be loaded: {reason}",
        "llm_timeout": "The language model was not ready after {seconds} s: {reason}",
        "gpu_full": "GPU memory full",
        "gpu_users": " (used by: {users})",
        "llm_http": "The language model reports an error ({status}): {detail}",
        "invented": "\n\n⚠ The answer cites {ids}, which did not come up in the search – "
                    "probably made up.",
        "invented_pl": "",
        "no_profile": "No Thunderbird profile found. Put its path into {file}: "
                      "profile = \"…\"",
        "voice_running": "already running",
        "voice_recording": "recording",
        "voice_cancelled": "cancelled",
        "voice_none": "no recording active",
        "voice_failed": "Speech recognition failed: {err}",
        "voice_no_recorder": "Cannot record: {err}",
    },
}

LANGUAGE_NAMES = {"de": "German", "en": "English"}


def norm(lang: str | None) -> str:
    lang = (lang or "").lower()
    return "de" if lang.startswith(("de", "german")) else "en"  # Windows: "German_Germany"


def system_lang() -> str:
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if os.environ.get(var):
            return norm(os.environ[var])
    try:
        return norm(locale.getlocale()[0])
    except Exception:
        return "en"


def t(lang: str, key: str, **kw) -> str:
    return TEXTS[norm(lang)][key].format(**kw)
