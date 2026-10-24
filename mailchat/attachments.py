"""Read attachments: from indexed mails (raw mbox) or from files given in the chat.

Text comes from the format itself where possible (PDF text layer, Word/OpenDocument
XML, calendar files). Scans and photos go to a vision-capable LLM if there is one,
else to Tesseract OCR. Every external tool is optional; without it that format
is reported as unreadable instead of failing.
"""
from __future__ import annotations

import base64
import email
import email.policy
import io
import json
import mmap
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import urllib.request
import zipfile
from collections import OrderedDict
from pathlib import Path
from xml.etree import ElementTree

from . import config
from .indexer import FROM_RE

MAX_CHARS = 20_000       # text of one attachment handed to the model
MAX_PAGES = 10           # pages of a scanned PDF that are OCRed
MAX_UPLOAD = 25 << 20    # bytes per file given in the chat
MAX_UPLOADS = 30         # files kept in memory, oldest dropped first
SMALL_IMAGE = 20_000     # bytes; smaller images are skipped unless asked for by name

_cache: OrderedDict[tuple, str] = OrderedDict()
_uploads: OrderedDict[str, dict] = OrderedDict()
_lock = threading.Lock()


# --------------------------------------------------------------------------- mails

def raw_message(con: sqlite3.Connection, mid: int) -> bytes | None:
    """The message as stored in Thunderbird's mbox, without the "From " line."""
    for row in con.execute("SELECT path, offset FROM loc WHERE msg=?", (int(mid),)):
        try:
            with open(row["path"], "rb") as fh, \
                    mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                start = row["offset"]
                if mm[start:start + 5] != b"From ":
                    continue  # mbox was compacted since the last index run
                nxt = FROM_RE.search(mm, start + 5)
                raw = mm[start:nxt.start() if nxt else len(mm)]
        except (OSError, ValueError):
            continue
        return raw[raw.find(b"\n") + 1:]
    return None


def _parts(raw: bytes) -> list[dict]:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        try:
            fname = part.get_filename()
            disp = part.get_content_disposition()
        except Exception:
            continue
        ctype = part.get_content_type()
        # Calendar invitations often come inline without a file name.
        if not fname and disp != "attachment" and ctype != "text/calendar":
            continue
        out.append({"name": " ".join(str(fname or "invite.ics").split()), "type": ctype,
                    "part": part})
    return out


def list_attachments(con: sqlite3.Connection, mid: int) -> list[dict]:
    raw = raw_message(con, mid)
    if raw is None:
        return []
    return [{"name": p["name"], "type": p["type"],
             "size": len(p["part"].get_payload(decode=True) or b"")} for p in _parts(raw)]


def read_attachment(con: sqlite3.Connection, mid: int, name: str = "") -> dict:
    """Text of one attachment of mail `mid` (all attachments if name is empty)."""
    raw = raw_message(con, mid)
    if raw is None:
        return {"error": f"mail {mid} not found in the mail folders (re-index running?)"}
    parts = _parts(raw)
    if not parts:
        return {"id": mid, "error": "this mail has no attachments"}
    if name:
        want = name.lower().strip()
        chosen = [p for p in parts if p["name"].lower() == want] or \
                 [p for p in parts if want in p["name"].lower()]
        if not chosen:
            return {"id": mid, "error": f"no attachment named {name!r}",
                    "attachments": [p["name"] for p in parts]}
    else:
        # Small inline images are logos and signatures, not content.
        chosen = [p for p in parts if not (_kind(p["name"], p["type"]) == "image"
                  and len(p["part"].get_payload(decode=True) or b"") < SMALL_IMAGE)][:5]
        if not chosen:
            return {"id": mid, "error": "only small inline images (logos) attached",
                    "attachments": [p["name"] for p in parts]}
    out = []
    for p in chosen:
        data = p["part"].get_payload(decode=True) or b""
        key = (mid, p["name"], len(data))
        out.append({"name": p["name"], "type": p["type"],
                    "text": _cached(key, lambda: extract_text(p["name"], p["type"], data))})
    return {"id": mid, "attachments": out}


def attachment_bytes(con: sqlite3.Connection, mid: int, name: str) -> tuple[bytes, str] | None:
    raw = raw_message(con, mid)
    for p in _parts(raw) if raw else []:
        if p["name"] == name:
            return p["part"].get_payload(decode=True) or b"", p["type"]
    return None


def _cached(key: tuple, fn) -> str:
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    text = fn()
    with _lock:
        _cache[key] = text
        while len(_cache) > 100:
            _cache.popitem(last=False)
    return text


# --------------------------------------------------------------------------- uploads

def add_upload(name: str, ctype: str, data_b64: str) -> dict:
    data = base64.b64decode(data_b64)
    if len(data) > MAX_UPLOAD:
        return {"error": f"file too large ({len(data) >> 20} MB, max {MAX_UPLOAD >> 20} MB)"}
    fid = "f" + secrets.token_hex(4)
    text = extract_text(name, ctype or "", data)
    with _lock:
        _uploads[fid] = {"id": fid, "name": name, "type": ctype, "size": len(data), "text": text}
        while len(_uploads) > MAX_UPLOADS:
            _uploads.popitem(last=False)
    return {"id": fid, "name": name, "size": len(data), "chars": len(text),
            "readable": not text.startswith("(")}


def read_upload(fid: str) -> dict:
    with _lock:
        f = _uploads.get(fid)
    if not f:
        return {"error": f"no file {fid!r} in this chat (the server was restarted?)"}
    return {k: f[k] for k in ("id", "name", "type", "text")}


def upload_names(fids: list[str]) -> dict[str, str]:
    with _lock:
        return {fid: _uploads[fid]["name"] for fid in fids if fid in _uploads}


# --------------------------------------------------------------------------- formats

def _kind(name: str, ctype: str) -> str:
    ext = Path(name.lower()).suffix
    if ext == ".pdf" or ctype == "application/pdf":
        return "pdf"
    if ext in (".docx", ".docm", ".dotx", ".odt", ".ott", ".pptx", ".odp", ".xlsx", ".ods"):
        return "office-xml"
    if ext in (".doc", ".rtf", ".ppt", ".xls", ".pages", ".wpd"):
        return "office-old"
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic") \
            or ctype.startswith("image/"):
        return "image"
    if ext in (".ics", ".vcs") or ctype == "text/calendar":
        return "text"
    if ext in (".txt", ".csv", ".md", ".eml", ".vcf", ".html", ".htm", ".xml", ".json") \
            or ctype.startswith("text/"):
        return "text"
    return "other"


def extract_text(name: str, ctype: str, data: bytes) -> str:
    """Readable text of a file, or a message in parentheses why there is none."""
    kind = _kind(name, ctype)
    try:
        if kind == "pdf":
            text = _pdf(data)
        elif kind == "office-xml":
            text = _office_xml(data)
        elif kind == "office-old":
            text = _libreoffice(name, data)
        elif kind == "image":
            text = _image(data, ctype)
        elif kind == "text":
            text = data.decode("utf-8", "replace")
            if Path(name.lower()).suffix in (".html", ".htm"):
                from .extract import html_to_text
                text = html_to_text(text)[0]
        else:
            return f"(cannot read files of type {ctype or Path(name).suffix})"
    except Exception as e:
        return f"(could not read {name}: {type(e).__name__}: {e})"
    text = re.sub(r"[ \t]+", " ", text or "")
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    if not text:
        return "(no text found)"
    return text[:MAX_CHARS] + (" …[truncated]" if len(text) > MAX_CHARS else "")


def _run(cmd: list[str], data: bytes | None = None, timeout: int = 120) -> bytes:
    return subprocess.run(cmd, input=data, capture_output=True, timeout=timeout,
                          check=True).stdout


def _pdf(data: bytes) -> str:
    text = ""
    if shutil.which("pdftotext"):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "in.pdf")
            src.write_bytes(data)
            text = _run(["pdftotext", "-layout", "-l", "30", str(src), "-"]).decode(
                "utf-8", "replace")
    else:
        try:
            import pypdf  # optional
            reader = pypdf.PdfReader(io.BytesIO(data))
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:30])
        except ImportError:
            pass
    if len(text.strip()) >= 40:
        return text
    # No text layer: a scan. Render the pages and read them like images.
    if not shutil.which("pdftoppm"):
        return text or "(scanned PDF: install poppler (pdftoppm) and Tesseract to read it)"
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "in.pdf")
        src.write_bytes(data)
        _run(["pdftoppm", "-r", "200", "-l", str(MAX_PAGES), "-png", str(src),
              str(Path(tmp, "p"))], timeout=300)
        pages = sorted(Path(tmp).glob("p*.png"))
        return "\n\n".join(_image(p.read_bytes(), "image/png") for p in pages)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _office_xml(data: bytes) -> str:
    """Word/PowerPoint/Excel (OOXML) and OpenDocument: text of the XML parts."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        if "content.xml" in names:  # OpenDocument
            return _xml_text(z.read("content.xml"), para=("}p", "}h", "}table-row"))
        parts = [n for n in names if re.match(
            r"(word/(document|header\d*|footer\d*)|ppt/slides/slide\d+|xl/sharedStrings)\.xml$",
            n)]
        return "\n\n".join(_xml_text(z.read(n), para=("}p", "}tr", "}si")) for n in sorted(parts))


def _xml_text(xml: bytes, para: tuple[str, ...]) -> str:
    out: list[str] = []

    def walk(el) -> None:
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag.endswith(("}tab", "}s")):
            out.append(" ")
        elif tag.endswith(("}br", "}line-break")):
            out.append("\n")
        if el.text and tag.endswith(("}t", "}p", "}h", "}span", "}a")):
            out.append(el.text)
        for child in el:
            walk(child)
            if child.tail and tag.endswith(("}p", "}h", "}span", "}a")):
                out.append(child.tail)
        if tag.endswith(para):
            out.append("\n")
        elif tag.endswith(("}tc", "}table-cell", "}c")):
            out.append(" | ")

    walk(ElementTree.fromstring(xml))
    return "".join(out)


def _libreoffice(name: str, data: bytes) -> str:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        return f"(cannot read {Path(name).suffix} files: LibreOffice is not installed)"
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "in" + Path(name).suffix.lower())
        src.write_bytes(data)
        # Own profile dir: a running LibreOffice would otherwise swallow the job.
        _run([exe, f"-env:UserInstallation=file://{Path(tmp, 'profile').as_posix()}",
              "--headless", "--convert-to", "txt:Text (encoded):UTF8", "--outdir", tmp,
              str(src)], timeout=180)
        out = Path(tmp, "in.txt")
        return out.read_text("utf-8", "replace") if out.exists() else ""


# --------------------------------------------------------------------------- images

VISION_PROMPT = ("Transcribe all text in this image exactly, line by line, in its original "
                 "language. Then describe in one or two sentences what the image shows. "
                 "Keep dates, times, places and names exactly as written.")


def _image(data: bytes, ctype: str) -> str:
    vision_error = "vision = off"
    if config.VISION != "off":
        try:
            return _vision(data, ctype)
        except Exception as e:
            # The configured model may simply not see images; OCR is the fallback.
            vision_error = f"{type(e).__name__}: {e}"
    if shutil.which("tesseract"):
        langs = _tesseract_langs()
        return _run(["tesseract", "stdin", "stdout", "-l", langs], data, timeout=180).decode(
            "utf-8", "replace")
    return f"(cannot read images: no vision model ({vision_error}) and no Tesseract OCR)"


def _tesseract_langs() -> str:
    try:
        have = set(_run(["tesseract", "--list-langs"], timeout=10).decode().split()[1:])
    except Exception:
        have = set()
    langs = [l for l in ("deu", "eng") if l in have]
    return "+".join(langs) or "eng"


def _vision(data: bytes, ctype: str) -> str:
    if not ctype.startswith("image/") or ctype in ("image/tiff", "image/bmp", "image/heic"):
        ctype = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
    url = f"data:{ctype};base64,{base64.b64encode(data).decode()}"
    body = {"model": config.VISION_MODEL or config.LLM_MODEL, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "none",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": VISION_PROMPT},
                {"type": "image_url", "image_url": {"url": url}}]}]}
    headers = {"Content-Type": "application/json"}
    if config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"
    req = urllib.request.Request(config.VISION_URL or config.LLM_URL,
                                 data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=300) as resp:
        text = json.loads(resp.read())["choices"][0]["message"].get("content") or ""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
