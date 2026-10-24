"""mailchat — chat with your own Thunderbird mailbox, locally.

  mailchat doctor                check profile, index, LLM and voice input
  mailchat index [--full]        update the index (or rebuild it completely)
  mailchat search WORDS          full-text search without the LLM
  mailchat ask QUESTION          one question to the LLM, answer in the terminal
  mailchat chat                  interactive chat in the terminal
  mailchat serve [--no-reindex]  HTTP API for the Thunderbird add-on (127.0.0.1)
  mailchat autostart [--remove]  start the server at login (Linux: systemd, Windows: Startup folder)
  mailchat config                show the settings in effect and where the config file lives

Instead of `mailchat` you can always run `python -m mailchat`.
"""
from __future__ import annotations

import sys

from . import agent, config, i18n, indexer, server
from .search import search


def _run_chat(history: list[dict]) -> str:
    answer = ""
    lang = i18n.system_lang()
    for ev in agent.chat(indexer.connect(readonly=True), history, lang):
        if ev["type"] == "status":
            print(f"  \033[2m{ev['text']}\033[0m", file=sys.stderr)
        elif ev["type"] == "tool":
            args = ", ".join(f"{k}={v!r}" for k, v in ev["args"].items())
            print(f"  \033[2m→ {ev['name']}({args})\033[0m", file=sys.stderr)
        elif ev["type"] == "hits":
            print(f"  \033[2m  {ev['count']} hits\033[0m", file=sys.stderr)
        elif ev["type"] == "answer":
            answer = ev["text"]
            print("\n" + answer)
            for m in ev["mails"]:
                print(f"  [#{m['id']}] {m['date']}  {m['from']}  — {m['subject']}")
    return answer


def _show_config() -> None:
    print(f"config file:  {config.CONFIG_FILE}" +
          ("" if config.CONFIG_FILE.exists() else "  (not present, defaults in use)"))
    print(f"profile:      {config.PROFILE}")
    print(f"index:        {config.DB_PATH}")
    print(f"llm_url:      {config.LLM_URL}")
    print(f"llm_model:    {config.LLM_MODEL}")
    if config.LLM_UNIT:
        print(f"llm_unit:     {config.LLM_UNIT} (idle stop after {config.LLM_IDLE} s)")
    print(f"port:         {config.PORT}")
    print(f"whisper_url:  {config.WHISPER_URL}")


def _doctor() -> int:
    from . import llmhost, voice
    bad = 0

    def line(good: bool, text: str, fix: str = "") -> None:
        nonlocal bad
        bad += not good
        print(("  ok   " if good else "  !!   ") + text)
        if fix and not good:
            print("       → " + fix)

    prof = config.PROFILE
    line(bool(prof and prof.is_dir()), f"Thunderbird profile: {prof}",
         f'set profile = "…" in {config.CONFIG_FILE}')
    roots = [r for r in config.MAIL_ROOTS if r.is_dir()]
    line(bool(roots), "mail folders: " + (", ".join(str(r) for r in roots) or "none"),
         "Thunderbird must keep mail locally: account settings → Synchronization & Storage")
    try:
        n = indexer.connect(readonly=True).execute("SELECT count(*) FROM msgs").fetchone()[0]
    except Exception:
        n = 0
    line(n > 0, f"index: {n} mails in {config.DB_PATH}", "run: mailchat index")
    line(llmhost.ok() or bool(config.LLM_UNIT), f"LLM: {config.LLM_MODEL} @ {config.LLM_URL}",
         "start Ollama and run: ollama pull " + config.LLM_MODEL)
    if voice.whisper_ok():
        line(voice.available(), "voice input: whisper server answers, recorder "
             + ("found" if voice.available() else "missing"), "pip install sounddevice")
    else:
        print("  --   voice input off (no whisper server at " + config.WHISPER_URL + ")")
    # Attachments: each tool is optional, without it that format stays unreadable.
    import shutil
    for exe, what in (("pdftotext", "PDF attachments (poppler-utils)"),
                      ("tesseract", "scans and photos without a vision model (Tesseract OCR)"),
                      ("soffice", "old Word/Excel files .doc/.xls/.rtf (LibreOffice)")):
        found = shutil.which(exe) or (exe == "soffice" and shutil.which("libreoffice"))
        print(("  ok   " if found else "  --   ") + what + ("" if found else ": not installed"))
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "index":
        indexer.run(full="--full" in rest)
    elif cmd == "search":
        res = search(indexer.connect(readonly=True), " ".join(rest), limit=15)
        for r in res["results"]:
            print(f"#{r['id']:<7} {r['date']}  {r['from'][:40]:40}  {r['subject'][:70]}")
            for l in r["matching_links"]:
                print(f"         {l[:150]}")
    elif cmd == "ask":
        _run_chat([{"role": "user", "content": " ".join(rest)}])
    elif cmd == "chat":
        history: list[dict] = []
        while True:
            try:
                q = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                return 0
            if not q:
                continue
            history.append({"role": "user", "content": q})
            history.append({"role": "assistant", "content": _run_chat(history)})
    elif cmd == "serve":
        server.serve(reindex="--no-reindex" not in rest)
    elif cmd == "autostart":
        from . import autostart
        print(autostart.remove() if "--remove" in rest else autostart.install())
    elif cmd == "config":
        _show_config()
    elif cmd == "doctor":
        return _doctor()
    else:
        print(__doc__)
        return 2
    return 0


def run() -> None:
    """Entry point of the `mailchat` command."""
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":
    run()
