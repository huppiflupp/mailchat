"""Things the chat can do besides answering: find addresses, prepare calendar
events and mail drafts.

The server never writes to the calendar or sends mail itself. It checks what the
model asked for and hands it to the add-on, which opens Thunderbird's own event
dialog or compose window. The user saves or sends there.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import re
import sqlite3

from . import attachments, config

_PREF_RE = re.compile(r'user_pref\("mail\.identity\.[^.]+\.useremail",\s*"([^"]+)"\)')


def own_addresses() -> set[str]:
    """The user's addresses: the identities in Thunderbird's prefs.js."""
    try:
        prefs = (config.PROFILE / "prefs.js").read_text("utf-8", "replace")
    except (OSError, TypeError):
        return set()
    return {a.lower() for a in _PREF_RE.findall(prefs)}


def find_contact(con: sqlite3.Connection, name: str, limit: int = 5) -> dict:
    """Addresses whose name or address contains every word of `name`,
    the ones in the most (and most recent) mails first."""
    words = [w.lower() for w in re.findall(r"[\w@.-]+", name or "") if len(w) >= 2]
    if not words:
        return {"error": "give a name or part of an address"}
    like = f"%{max(words, key=len)}%"
    found: dict[str, dict] = {}
    rows = con.execute("SELECT sender, recipients, date FROM msgs WHERE sender LIKE ?"
                       " OR recipients LIKE ? ORDER BY date DESC LIMIT 2000", (like, like))
    for r in rows:
        for display, addr in email.utils.getaddresses([r["sender"] or "", r["recipients"] or ""]):
            addr = addr.strip().lower()
            if "@" not in addr:
                continue
            hay = f"{display} {addr}".lower()
            if not all(w in hay for w in words):
                continue
            c = found.setdefault(addr, {"address": addr, "name": display.strip(),
                                        "mails": 0, "last": r["date"] or 0})
            c["mails"] += 1
            if display.strip() and not c["name"]:
                c["name"] = display.strip()
    best = sorted(found.values(), key=lambda c: (-c["mails"], -c["last"]))[:limit]
    mine = own_addresses()
    for c in best:
        c["last"] = _day(c["last"])
        if c["address"] in mine:
            c["note"] = "this is the user's own address"
    if best:
        return {"count": len(best), "contacts": best}
    # Not in any header: the name may stand in the signature of mails from an
    # address that does not contain it (asp-kv-has@web.de for Herbert Hausmann).
    maybe = [{"address": r["sender"], "mails": r["n"], "last": _day(r["last"])}
             for r in con.execute(
                 "SELECT m.sender, count(*) AS n, max(m.date) AS last FROM fts"
                 " JOIN msgs m ON m.id = fts.rowid WHERE fts MATCH ?"
                 " GROUP BY m.sender ORDER BY n DESC LIMIT ?",
                 ("body:(" + " AND ".join(f'"{w}"' for w in words) + ")", limit))]
    if maybe:
        return {"count": 0, "maybe": maybe,
                "hint": "no address carries this name, but it appears in the text of mails "
                        "from these senders (e.g. as signature). Ask the user before using one."}
    return {"count": 0, "hint": "no address found; try the last name alone or ask the user"}


def _day(ts: int | None) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?"


# --------------------------------------------------------------------------- events

def _parse_when(value: str) -> tuple[dt.datetime, bool]:
    """'2026-11-15', '2026-11-15T18:30', '2026-11-15 18:30' -> (datetime, date_only)."""
    value = (value or "").strip().replace(" ", "T")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return dt.datetime.fromisoformat(value), True
    when = dt.datetime.fromisoformat(value[:16])
    return when, False


def make_event(args: dict) -> dict:
    """Validated event for the add-on, or {"error": …} for the model to fix.

    Never carries attendees: the event is for the user's own calendar only."""
    summary = " ".join(str(args.get("summary") or "").split())
    if not summary:
        return {"error": "summary is missing"}
    try:
        start, date_only = _parse_when(str(args.get("start", "")))
    except ValueError:
        return {"error": "start must be YYYY-MM-DD or YYYY-MM-DDTHH:MM"}
    all_day = bool(args.get("all_day")) or date_only
    end = None
    if args.get("end"):
        try:
            end, _ = _parse_when(str(args["end"]))
        except ValueError:
            return {"error": "end must be YYYY-MM-DD or YYYY-MM-DDTHH:MM"}
    if all_day:
        start = start.replace(hour=0, minute=0)
        # iCalendar: the end of an all-day event is the day after the last day.
        end = (end.replace(hour=0, minute=0) if end else start) + dt.timedelta(days=1)
    elif end is None or end <= start:
        end = start + dt.timedelta(hours=1)
    fmt = "%Y%m%dT%H%M%S"
    return {
        "summary": summary[:300],
        "startDate": start.strftime(fmt),
        "endDate": end.strftime(fmt),
        "forceAllDay": all_day,
        "location": str(args.get("location") or "")[:500],
        "description": str(args.get("description") or "")[:4000],
        "attendees": [],
        # For the chat bubble; the add-on drops these before opening the dialog.
        "start_text": start.strftime("%Y-%m-%d") if all_day else start.strftime("%Y-%m-%d %H:%M"),
        "end_text": (end - dt.timedelta(days=1)).strftime("%Y-%m-%d") if all_day
        else end.strftime("%Y-%m-%d %H:%M"),
    }


# --------------------------------------------------------------------------- drafts

_ADDR_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


def make_draft(con: sqlite3.Connection, args: dict) -> dict:
    """Validated mail draft for the add-on, or {"error": …} for the model to fix."""
    to = args.get("to") or []
    if isinstance(to, str):
        to = re.split(r"[,;]\s*", to)
    addrs = []
    for t in to:
        _name, addr = email.utils.parseaddr(str(t))
        if not _ADDR_RE.match(addr):
            return {"error": f"{t!r} is not a mail address; look it up with find_contact"}
        addrs.append(str(t).strip())
    if not addrs:
        return {"error": "to is missing; look the address up with find_contact"}

    files = []
    names = attachments.upload_names([str(f) for f in args.get("files") or []])
    for fid in args.get("files") or []:
        if str(fid) not in names:
            return {"error": f"there is no attached file {fid!r}"}
        files.append({"file": str(fid), "name": names[str(fid)]})
    for ref in args.get("mail_attachments") or []:
        mid, name = int(ref.get("id", 0)), str(ref.get("name", ""))
        have = [a["name"] for a in attachments.list_attachments(con, mid)]
        match = [a for a in have if a == name] or [a for a in have if name.lower() in a.lower()]
        if not match:
            return {"error": f"mail {mid} has no attachment {name!r}", "attachments": have}
        row = con.execute("SELECT msgid FROM msgs WHERE id=?", (mid,)).fetchone()
        files.append({"mail": mid, "msgid": row["msgid"], "name": match[0]})
    return {"to": addrs, "subject": str(args.get("subject") or "")[:300],
            "body": str(args.get("body") or ""), "attachments": files}
