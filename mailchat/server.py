"""Local HTTP API for the Thunderbird add-on (127.0.0.1 only).

Mail content must not leak to web pages open in some browser, so requests are
refused unless they come from a Thunderbird extension (Origin moz-extension://…)
or from a local tool without an Origin header (curl, CLI), and the Host header
must name localhost (blocks DNS rebinding).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import __version__, agent, attachments, config, i18n, indexer, llmhost, update, voice
from .search import read_mail, search

_state = {"last_index": None, "last_stats": None, "indexing": False, "index_error": None}


def _db() -> sqlite3.Connection:
    return indexer.connect(readonly=True)


def _reindex_loop() -> None:
    while True:
        _index_once()
        time.sleep(config.REINDEX_INTERVAL)


def _index_once() -> None:
    _state["indexing"] = True
    try:
        _state["last_stats"] = indexer.run(quiet=True)
        _state["last_index"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _state["index_error"] = None
    except Exception as e:
        traceback.print_exc()
        _state["index_error"] = str(e)
    finally:
        _state["indexing"] = False


class Handler(BaseHTTPRequestHandler):
    server_version = "mailchat/1"

    def log_message(self, fmt, *args):  # quieter journal
        if not self.path.startswith("/api/status"):
            super().log_message(fmt, *args)

    # ---------------------------------------------------------------- guards
    def _allowed(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.startswith("moz-extension://")

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin and origin.startswith("moz-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, obj, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _lang(self, body: dict | None = None) -> str:
        """Language for user-facing texts: "lang" from the add-on, else Accept-Language."""
        return i18n.norm((body or {}).get("lang") or self.headers.get("Accept-Language"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    # ---------------------------------------------------------------- verbs
    def do_OPTIONS(self):
        if not self._allowed():
            return self._json({"error": "forbidden"}, 403)
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if not self._allowed():
            return self._json({"error": "forbidden"}, 403)
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/status":
            try:
                with _db() as con:
                    total = con.execute("SELECT count(*) FROM msgs").fetchone()[0]
            except sqlite3.Error:
                total = 0
            # In parallel: on Windows a refused connection to localhost takes ~2 s each.
            with ThreadPoolExecutor(2) as ex:
                llm, whisper = ex.submit(llmhost.ok), ex.submit(voice.whisper_ok)
                llm, whisper = llm.result(), whisper.result() and voice.available()
            return self._json({"version": __version__, "mails": total, "llm": llm, "llm_on_demand": bool(config.LLM_UNIT),
                               "llm_model": config.LLM_MODEL, "profile": str(config.PROFILE or ""),
                               "whisper": whisper,
                               "recording": voice.recording(), **_state})
        if u.path == "/api/search":
            with _db() as con:
                return self._json(search(con, q.get("q", ""), sender=q.get("sender"),
                                         after=q.get("after"), before=q.get("before"),
                                         sort=q.get("sort", "relevance"),
                                         limit=int(q.get("limit", 20))))
        if u.path == "/api/update":
            # Only on request: the one place where mailchat goes to the internet.
            try:
                return self._json(update.check())
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"})
        if u.path.startswith("/api/mail/"):
            with _db() as con:
                return self._json(read_mail(con, int(u.path.rsplit("/", 1)[1])))
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._allowed():
            return self._json({"error": "forbidden"}, 403)
        u = urlsplit(self.path)
        if u.path == "/api/reindex":
            if _state["indexing"]:
                return self._json({"status": "running"})
            threading.Thread(target=_index_once, daemon=True).start()
            return self._json({"status": "started"})
        if u.path == "/api/voice/start":
            return self._json(voice.start(self._lang()))
        if u.path == "/api/voice/stop":
            return self._json(voice.stop(self._lang()))
        if u.path == "/api/voice/cancel":
            return self._json(voice.cancel(self._lang()))
        if u.path == "/api/chat":
            return self._chat(self._body())
        if u.path == "/api/update":
            res = update.install(str(self._body().get("tag") or ""))
            if res.get("ok"):
                update.restart_later(busy=lambda: _state["indexing"])
            return self._json(res)
        if u.path == "/api/file":
            # A file dropped into the chat: its text is extracted once and kept in
            # memory, the model reads it by id. The file itself stays in the add-on.
            b = self._body()
            try:
                return self._json(attachments.add_upload(str(b.get("name") or "file"),
                                                         str(b.get("type") or ""),
                                                         str(b.get("data") or "")))
            except (ValueError, TypeError) as e:
                return self._json({"error": str(e)}, 400)
        self._json({"error": "not found"}, 404)

    def _chat(self, req: dict) -> None:
        """Streams newline-delimited JSON events (tool, hits, answer | error)."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()

        def emit(ev: dict) -> None:
            self.wfile.write((json.dumps(ev, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()

        try:
            with _db() as con:
                for ev in agent.chat(con, req.get("messages") or [], self._lang(req)):
                    emit(ev)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            traceback.print_exc()
            if isinstance(e, RuntimeError):
                text = str(e)
            elif isinstance(e, urllib.error.HTTPError):
                text = i18n.t(self._lang(req), "llm_http", status=e.code,
                              detail=e.read().decode(errors="replace")[:300])
            elif isinstance(e, urllib.error.URLError):
                text = i18n.t(self._lang(req), "llm_unreachable", url=config.LLM_URL,
                              model=config.LLM_MODEL)
            else:
                text = f"{type(e).__name__}: {e}"
            try:
                emit({"type": "error", "text": text})
            except OSError:
                pass


def serve(reindex: bool = True) -> None:
    if sys.stdout is None:  # pythonw.exe (Windows autostart) has no console
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(config.DATA_DIR / "server.log", "a",
                                       encoding="utf-8", buffering=1)
    indexer.connect().close()  # make sure the schema exists before read-only opens
    if reindex:
        threading.Thread(target=_reindex_loop, daemon=True).start()
    httpd = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    print(f"mailchat: http://{config.HOST}:{config.PORT}  profile: {config.PROFILE}  "
          f"LLM: {config.LLM_MODEL} @ {config.LLM_URL}", flush=True)
    httpd.serve_forever()
