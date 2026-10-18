"""Read Thunderbird's mbox files (read-only) into a SQLite FTS5 index.

Incremental: mbox files only grow between compactions, so a file whose first
64 KiB are unchanged and which got longer is parsed from the old end onward.
Anything else (compaction, shrink) re-reads that file. Messages are keyed by
Message-ID, so Gmail's copies in "Alle Nachrichten", labels etc. are stored once
and only gain another folder entry.
"""
from __future__ import annotations

import hashlib
import mmap
import multiprocessing as mp
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from . import config
from .extract import parse_message

SKIP_SUFFIXES = (".msf", ".dat", ".html", ".json", ".sqlite", ".log", ".js", ".txt")
FROM_RE = re.compile(rb"(?m)^From [^\n]*\n(?=[\w-]+:)")
BATCH_BYTES = 8 << 20
HEAD_BYTES = 64 << 10

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS msgs(
    id INTEGER PRIMARY KEY,
    msgid TEXT UNIQUE NOT NULL,
    date INTEGER, sender TEXT, recipients TEXT, subject TEXT,
    links TEXT, attachments TEXT);
CREATE INDEX IF NOT EXISTS msgs_date ON msgs(date);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    subject, sender, recipients, body, links, attachments,
    tokenize='unicode61 remove_diacritics 2', prefix='3 5');
CREATE TABLE IF NOT EXISTS loc(
    path TEXT NOT NULL, offset INTEGER NOT NULL, msg INTEGER NOT NULL,
    PRIMARY KEY(path, offset));
CREATE INDEX IF NOT EXISTS loc_msg ON loc(msg);
CREATE TABLE IF NOT EXISTS files(
    path TEXT PRIMARY KEY, size INTEGER, mtime REAL, headhash TEXT);
"""


def _lock(path: Path):
    """Exclusive, non-blocking lock on path; None if another indexer holds it.
    The lock lives as long as the returned file object."""
    fh = open(path, "a+")
    try:
        if config.WINDOWS:
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def connect(readonly: bool = False) -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if readonly:
        con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True,
                              check_same_thread=False)
    else:
        con = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        con.executescript(SCHEMA)
    con.row_factory = sqlite3.Row
    return con


def folder_name(path: str) -> str:
    """'…/ImapMail/imap.example.com/Work.sbd/done' -> 'imap.example.com/Work/done'."""
    p = Path(path)
    for root in config.MAIL_ROOTS:
        try:
            rel = p.relative_to(root)
            break
        except ValueError:
            continue
    else:
        rel = p
    return rel.as_posix().replace(".sbd/", "/")


def find_mboxes() -> list[Path]:
    out = []
    for root in config.MAIL_ROOTS:
        if not root.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.endswith(SKIP_SUFFIXES) or ".bak" in f or f.startswith("."):
                    continue
                if f.lower() in config.SKIP_FOLDERS:
                    continue
                p = Path(dirpath, f)
                try:
                    if p.stat().st_size == 0:
                        continue
                    with p.open("rb") as fh:
                        if fh.read(5) != b"From ":
                            continue
                except OSError:
                    continue
                out.append(p)
    return out


def _headhash(p: Path) -> str:
    with p.open("rb") as fh:
        return hashlib.sha1(fh.read(HEAD_BYTES)).hexdigest()


# --------------------------------------------------------------------------- workers

def _parse_batch(job: tuple[str, list[tuple[int, int]]]):
    path, spans = job
    out = []
    with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        for start, end in spans:
            raw = mm[start:end]
            nl = raw.find(b"\n")
            try:
                rec = parse_message(raw[nl + 1:])
            except Exception as e:  # one broken message must not stop the run
                print(f"  ! {path}@{start}: {e}", file=sys.stderr)
                rec = None
            out.append((start, rec))
    return path, out


def _batches(path: Path, start: int, end: int):
    """Yield (path, [(msg_start, msg_end), …]) jobs of roughly BATCH_BYTES."""
    with path.open("rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        end = min(end, len(mm))
        starts = [m.start() for m in FROM_RE.finditer(mm, start, end)]
    if not starts:
        return
    spans = list(zip(starts, starts[1:] + [end]))
    batch, size = [], 0
    for s, e in spans:
        batch.append((s, e))
        size += e - s
        if size >= BATCH_BYTES or len(batch) >= 400:
            yield str(path), batch
            batch, size = [], 0
    if batch:
        yield str(path), batch


# --------------------------------------------------------------------------- main

def _store(con: sqlite3.Connection, path: str, results) -> int:
    new = 0
    for offset, rec in results:
        if rec is None:
            continue
        row = con.execute("SELECT id FROM msgs WHERE msgid=?", (rec["msgid"],)).fetchone()
        if row:
            mid = row[0]
        else:
            cur = con.execute(
                "INSERT INTO msgs(msgid,date,sender,recipients,subject,links,attachments)"
                " VALUES(?,?,?,?,?,?,?)",
                (rec["msgid"], rec["date"], rec["sender"], rec["recipients"],
                 rec["subject"], rec["links"], rec["attachments"]))
            mid = cur.lastrowid
            con.execute(
                "INSERT INTO fts(rowid,subject,sender,recipients,body,links,attachments)"
                " VALUES(?,?,?,?,?,?,?)",
                (mid, rec["subject"], rec["sender"], rec["recipients"], rec["body"],
                 rec["links"], rec["attachments"]))
            new += 1
        con.execute("INSERT OR REPLACE INTO loc(path,offset,msg) VALUES(?,?,?)",
                    (path, offset, mid))
    return new


def _gc(con: sqlite3.Connection) -> int:
    orphans = [r[0] for r in con.execute(
        "SELECT id FROM msgs WHERE id NOT IN (SELECT msg FROM loc)")]
    for mid in orphans:
        con.execute("DELETE FROM fts WHERE rowid=?", (mid,))
        con.execute("DELETE FROM msgs WHERE id=?", (mid,))
    return len(orphans)


def run(full: bool = False, workers: int | None = None, quiet: bool = False) -> dict:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if config.PROFILE is None or not any(r.is_dir() for r in config.MAIL_ROOTS):
        raise RuntimeError(f"No Thunderbird profile with mail found ({config.PROFILE}). "
                           f"Set profile = \"…\" in {config.CONFIG_FILE}")
    lock = _lock(config.DATA_DIR / "index.lock")
    if lock is None:
        if not quiet:
            print("Indexer is already running.", file=sys.stderr)
        return {"skipped": True}

    log = (lambda *a: None) if quiet else (lambda *a: print(*a, file=sys.stderr, flush=True))
    t0 = time.time()
    con = connect()
    known = {r["path"]: r for r in con.execute("SELECT * FROM files")}
    mboxes = find_mboxes()
    present = {str(p) for p in mboxes}

    # Folders that disappeared (deleted/renamed in Thunderbird).
    for gone in set(known) - present:
        con.execute("DELETE FROM loc WHERE path=?", (gone,))
        con.execute("DELETE FROM files WHERE path=?", (gone,))

    jobs = []  # (path, start_offset, size, mtime, headhash)
    for p in mboxes:
        st = p.stat()
        old = known.get(str(p))
        hh = _headhash(p)
        if not full and old and old["size"] == st.st_size and old["mtime"] == st.st_mtime:
            continue
        if not full and old and st.st_size > old["size"] and old["headhash"] == hh:
            start = old["size"]
        else:
            start = 0
            con.execute("DELETE FROM loc WHERE path=?", (str(p),))
        jobs.append((p, start, st.st_size, st.st_mtime, hh))
    con.commit()

    total_bytes = sum(size - start for _p, start, size, _m, _h in jobs)
    log(f"{len(mboxes)} folders, {len(jobs)} changed, {total_bytes / 1e9:.2f} GB to read")

    new = 0
    if jobs:
        # forkserver does not exist on Windows; spawn works everywhere, just starts slower.
        ctx = mp.get_context("spawn" if config.WINDOWS or config.MACOS else "forkserver")
        with ctx.Pool(workers or max(1, (os.cpu_count() or 2) - 2)) as pool:
            for p, start, size, mtime, hh in jobs:
                log(f"  {folder_name(str(p))}  ({(size - start) / 1e6:.0f} MB)")
                for path, results in pool.imap(_parse_batch, _batches(p, start, size)):
                    new += _store(con, path, results)
                con.execute("INSERT OR REPLACE INTO files(path,size,mtime,headhash)"
                            " VALUES(?,?,?,?)", (str(p), size, mtime, hh))
                con.commit()
    removed = _gc(con)
    con.commit()
    if new > 2000 or removed > 2000:
        con.execute("INSERT INTO fts(fts) VALUES('optimize')")
        con.commit()
    count = con.execute("SELECT count(*) FROM msgs").fetchone()[0]
    con.close()
    stats = {"new": new, "removed": removed, "total": count,
             "seconds": round(time.time() - t0, 1)}
    log(f"done: {new} new, {removed} removed, {count} mails in the index, {stats['seconds']} s")
    return stats


if __name__ == "__main__":
    run(full="--full" in sys.argv)
