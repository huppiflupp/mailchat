"""Queries against the index: full-text search and reading single mails."""
from __future__ import annotations

import datetime as dt
import re
import sqlite3

from .indexer import folder_name

# bm25 weights: subject, sender, recipients, body, links, attachments
_RANK = "bm25(fts, 10.0, 6.0, 1.0, 1.0, 3.0, 6.0)"
_TOKEN_RE = re.compile(r"[\w@.-]+", re.UNICODE)


def _fts_query(text: str, op: str) -> str:
    terms = []
    for tok in _TOKEN_RE.findall(text):
        tok = tok.strip(".-")
        if len(tok) < 2:
            continue
        quoted = '"' + tok.replace('"', "") + '"'
        # Prefix match catches inflections and compounds starting with the word
        # (antrag -> antrags, antragsbuch). Not for short words or addresses.
        if len(tok) >= 4 and not re.search(r"[@.]", tok):
            quoted += "*"
        terms.append(quoted)
    return f" {op} ".join(terms)


def _date(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(dt.datetime.fromisoformat(s).timestamp())
    except ValueError:
        return None


def _fmt_date(ts: int | None) -> str:
    try:
        return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"
    except (OSError, OverflowError, ValueError):  # Windows rejects dates before 1970
        return "?"


def folders_of(con: sqlite3.Connection, mid: int) -> list[str]:
    return sorted({folder_name(r[0]) for r in con.execute(
        "SELECT path FROM loc WHERE msg=?", (mid,))})


def search(con: sqlite3.Connection, query: str = "", sender: str | None = None,
           after: str | None = None, before: str | None = None,
           folder: str | None = None, sort: str = "relevance", limit: int = 10) -> dict:
    limit = max(1, min(int(limit or 10), 30))
    where, args = [], []
    if sender:
        where.append("(m.sender LIKE ? )")
        args.append(f"%{sender}%")
    if (a := _date(after)) is not None:
        where.append("m.date >= ?")
        args.append(a)
    if (b := _date(before)) is not None:
        where.append("m.date < ?")
        args.append(b)
    if folder:
        where.append("EXISTS (SELECT 1 FROM loc l WHERE l.msg=m.id AND"
                     " replace(l.path,'.sbd/','/') LIKE ?)")
        args.append(f"%{folder}%")

    rows, mode = [], "and"
    for op in ("AND", "OR"):
        q = _fts_query(query, op)
        if q:
            sql = (f"SELECT m.*, {_RANK} AS rank,"
                   " snippet(fts, 3, '«', '»', ' … ', 24) AS snip"
                   " FROM fts JOIN msgs m ON m.id = fts.rowid WHERE fts MATCH ?")
            qargs = [q] + args
        else:
            sql = "SELECT m.*, 0 AS rank, '' AS snip FROM msgs m WHERE 1"
            qargs = list(args)
        if where:
            sql += " AND " + " AND ".join(where)
        sql += " ORDER BY " + ("m.date DESC" if sort == "newest" or not q else "rank")
        sql += f" LIMIT {limit}"
        try:
            rows = con.execute(sql, qargs).fetchall()
        except sqlite3.OperationalError as e:
            return {"error": f"search error: {e}", "results": []}
        if rows or not q or len(q.split(" AND ")) < 2:
            break
        mode = "or"  # nothing matched all words: retry with any word

    words = [w.lower() for w in _TOKEN_RE.findall(query) if len(w) >= 4]
    results = []
    for r in rows:
        links = [l for l in (r["links"] or "").split("\n")
                 if l and any(w in l.lower() for w in words)][:5]
        results.append({
            "id": r["id"], "date": _fmt_date(r["date"]), "from": r["sender"],
            "subject": r["subject"], "folders": folders_of(con, r["id"]),
            "snippet": " ".join((r["snip"] or "").split())[:400],
            "matching_links": links,
            "attachments": [a for a in (r["attachments"] or "").split("\n") if a][:8],
        })
    return {"match": mode, "count": len(results), "results": results}


def read_mail(con: sqlite3.Connection, mid: int, max_chars: int = 8000) -> dict:
    r = con.execute("SELECT m.*, f.body FROM msgs m JOIN fts f ON f.rowid=m.id"
                    " WHERE m.id=?", (int(mid),)).fetchone()
    if not r:
        return {"error": f"no mail with id {mid}"}
    body = r["body"] or ""
    return {
        "id": r["id"], "date": _fmt_date(r["date"]), "from": r["sender"],
        "to": r["recipients"], "subject": r["subject"],
        "folders": folders_of(con, r["id"]),
        "attachments": [a for a in (r["attachments"] or "").split("\n") if a],
        "links": (r["links"] or "").split("\n")[:60],
        "body": body[:max_chars] + (" …[gekürzt]" if len(body) > max_chars else ""),
    }


def mail_ref(con: sqlite3.Connection, mid: int) -> dict | None:
    """Minimal data the Thunderbird add-on needs to open a mail."""
    r = con.execute("SELECT id, msgid, date, sender, subject FROM msgs WHERE id=?",
                    (int(mid),)).fetchone()
    if not r:
        return None
    return {"id": r["id"], "msgid": r["msgid"], "date": _fmt_date(r["date"]),
            "from": r["sender"], "subject": r["subject"],
            "folders": folders_of(con, r["id"])}
