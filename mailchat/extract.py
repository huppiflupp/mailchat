"""Turn one raw RFC 822 message into a flat record for the index.

Runs inside worker processes, so it must not touch the database.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import json
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

from .config import BODY_LIMIT

MAX_LINKS = 80
_URL_RE = re.compile(r"https?://[^\s<>\"'\])]+")
_EMBEDDED_URL_RE = re.compile(r"https?://[^\s\"'<>&]+", re.I)
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKS_RE = re.compile(r"\n\s*\n\s*\n+")


# --------------------------------------------------------------------------- links

def resolve_redirect(url: str) -> str:
    """Return the real target of a tracking/redirect link, or the link itself.

    Newsletter tools wrap every link (Dynamics: ?msdynmkt_target={"TargetUrl":…},
    Google: ?q=…, Outlook safelinks: ?url=…). The target is always an URL-encoded
    http(s) URL somewhere in the query string, sometimes encoded twice.
    """
    try:
        query = urlsplit(url).query
    except ValueError:
        return url
    if not query:
        return url
    text = query
    for _ in range(3):
        m = re.search(r'"TargetUrl"\s*:\s*"([^"]+)"', text)
        if m:
            return resolve_redirect(unquote(unquote(m.group(1))))
        m = _EMBEDDED_URL_RE.search(text)
        if m:
            return m.group(0)
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return url


def _keep_link(url: str, label: str) -> bool:
    low = url.lower()
    if low.startswith(("mailto:", "tel:")):
        return False
    if any(w in low for w in ("unsubscribe", "abmelden", "/optout", "pixel", "/open?", "tracking/open")):
        return False
    if re.search(r"\.(png|gif|jpe?g|svg|webp)(\?|$)", low) and not label:
        return False
    return True


# --------------------------------------------------------------------------- html

class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
             "table", "ul", "ol", "blockquote", "section", "article", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._href: str | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "title"):
            self._skip += 1
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._label = []
        elif tag == "img" and self._href is not None:
            alt = dict(attrs).get("alt")
            if alt:
                self._label.append(alt)
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._href is not None:
            label = " ".join("".join(self._label).split())
            self.links.append((label, self._href))
            self._href = None
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        self.out.append(data)
        if self._href is not None:
            self._label.append(data)


def html_to_text(html: str) -> tuple[str, list[tuple[str, str]]]:
    p = _HTMLText()
    try:
        p.feed(html)
        p.close()
    except Exception:  # broken markup: fall back to tag stripping
        return unescape(re.sub(r"<[^>]+>", " ", html)), []
    return "".join(p.out), p.links


def _tidy(text: str) -> str:
    text = text.replace("\xa0", " ").replace("​", "").replace("‌", "")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS_RE.sub("\n\n", text).strip()


# --------------------------------------------------------------------------- message

def _decode(part) -> str:
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return ""
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


def _hdr(msg, name: str) -> str:
    try:
        value = msg.get(name)
    except Exception:
        return ""
    return " ".join(str(value).split()) if value is not None else ""


def _plain_links(text: str) -> list[tuple[str, str]]:
    """Links in plain text, labelled with the words right before them."""
    out = []
    for m in _URL_RE.finditer(text):
        before = text[max(0, m.start() - 80):m.start()]
        before = before.rsplit("\n", 1)[-1].strip(" <[(:")
        out.append((before[-60:], m.group(0)))
    return out


def parse_message(raw: bytes) -> dict | None:
    head = raw[:4000]
    m = re.search(rb"^X-Mozilla-Status: *([0-9a-fA-F]{4})", head, re.M)
    if m and int(m.group(1), 16) & 0x0008:  # expunged, waiting for compaction
        return None

    msg = email.message_from_bytes(raw, policy=email.policy.default)

    subject = _hdr(msg, "subject")
    sender = _hdr(msg, "from")
    recipients = ", ".join(filter(None, (_hdr(msg, "to"), _hdr(msg, "cc"))))[:1000]
    msgid = _hdr(msg, "message-id").strip("<> ")
    date = 0
    try:
        dt = email.utils.parsedate_to_datetime(_hdr(msg, "date"))
        date = int(dt.timestamp())
    except Exception:
        pass
    if not msgid:
        msgid = "nomsgid-" + hashlib.sha1(
            f"{date}|{sender}|{subject}".encode()).hexdigest()

    plains: list[str] = []
    htmls: list[str] = []
    attachments: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        try:
            disp = part.get_content_disposition()
            fname = part.get_filename()
        except Exception:
            disp, fname = None, None
        if fname or disp == "attachment":
            if fname:
                attachments.append(" ".join(str(fname).split()))
            continue
        if ctype == "text/plain":
            plains.append(_decode(part))
        elif ctype == "text/html":
            htmls.append(_decode(part))

    links: list[tuple[str, str]] = []
    html_text = ""
    for h in htmls:
        t, ls = html_to_text(h)
        html_text += t + "\n"
        links += ls
    plain = "\n".join(plains)
    # Plain part is often a stub ("view in browser") in newsletters; prefer HTML then.
    body = plain if len(plain.strip()) > 0.3 * len(html_text.strip()) else html_text
    if not htmls:
        links = _plain_links(plain)
    body = _tidy(body)[:BODY_LIMIT]

    seen: set[str] = set()
    link_lines = []
    for label, url in links:
        if not url:
            continue
        url = unescape(url.strip())
        target = resolve_redirect(url) if url.lower().startswith("http") else url
        if not _keep_link(target, label) or target in seen:
            continue
        seen.add(target)
        label = " ".join(label.split())[:100]
        link_lines.append(f"{label} -> {target}" if label else target)
        if len(link_lines) >= MAX_LINKS:
            break

    return {
        "msgid": msgid[:500],
        "date": date,
        "sender": sender[:300],
        "recipients": recipients,
        "subject": subject[:500],
        "body": body,
        "links": "\n".join(link_lines),
        "attachments": "\n".join(attachments)[:2000],
    }


if __name__ == "__main__":  # debug: python -m mailchat.extract < message.eml
    import sys
    print(json.dumps(parse_message(sys.stdin.buffer.read()), ensure_ascii=False, indent=1))
