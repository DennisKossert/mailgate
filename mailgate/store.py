"""SQLite cache: messages, folders, watchers, drafts and audit log."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders(acct TEXT, name TEXT, uidvalidity INTEGER, last_uid INTEGER,
  PRIMARY KEY(acct, name));
CREATE TABLE IF NOT EXISTS msgs(id INTEGER PRIMARY KEY, acct TEXT, folder TEXT, uid INTEGER,
  uidvalidity INTEGER, msgid TEXT, irt TEXT, refs TEXT, thread TEXT, reply_to TEXT, date INTEGER,
  from_name TEXT, from_addr TEXT, to_addr TEXT, cc TEXT, subject TEXT, unread INTEGER, flags TEXT,
  atts TEXT, body TEXT, full TEXT, raw BLOB, size INTEGER,
  UNIQUE(acct, folder, uidvalidity, uid));
CREATE INDEX IF NOT EXISTS msgs_date ON msgs(date);
CREATE INDEX IF NOT EXISTS msgs_thread ON msgs(thread);
CREATE INDEX IF NOT EXISTS msgs_msgid ON msgs(msgid);
CREATE TABLE IF NOT EXISTS seen(watcher TEXT, msg INTEGER, PRIMARY KEY(watcher, msg));
CREATE TABLE IF NOT EXISTS watchers(name TEXT PRIMARY KEY, created INTEGER);
CREATE TABLE IF NOT EXISTS drafts(id INTEGER PRIMARY KEY, acct TEXT, created INTEGER, expires INTEGER,
  status TEXT, mime BLOB, sha256 TEXT, token_hash TEXT, sender TEXT, rcpts TEXT, to_addr TEXT,
  subject TEXT, reply_msg INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts INTEGER, draft INTEGER, action TEXT,
  via TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""
FIELDS = ("acct", "folder", "uid", "uidvalidity", "msgid", "irt", "refs", "thread", "reply_to", "date",
          "from_name", "from_addr", "to_addr", "cc", "subject", "unread", "flags", "atts", "body",
          "full", "raw", "size")
LIST_COLS = "id, acct, folder, date, from_name, from_addr, subject, unread, atts"


def b36(n: int) -> str:
    """Rowid -> short id."""
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    s = ""
    while True:
        n, r = divmod(n, 36)
        s = chars[r] + s
        if not n:
            return s


def parse_id(s: str, draft: bool = False) -> int:
    """Short id (mail 'k3f', draft 'd1') -> rowid. Trailing '*@' markers are ignored."""
    s = s.strip().rstrip("*@").lower()
    if draft:
        s = s.removeprefix("d")
    if not s or not re.fullmatch(r"[0-9a-z]+", s):
        raise ValueError(f"bad id: {s!r}")
    return int(s, 36)


def draft_ref(n: int) -> str:
    return "d" + b36(n)


def parse_since(s: str) -> int:
    """'7d', '24h', '2w', '30m' or 'YYYY-MM-DD' -> epoch seconds."""
    m = re.fullmatch(r"(\d+)([mhdw])", s.strip())
    if m:
        mult = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        return int(time.time()) - int(m.group(1)) * mult
    try:
        return int(datetime.strptime(s, "%Y-%m-%d").timestamp())
    except ValueError:
        raise ValueError(f"bad --since value: {s!r} (use 7d, 24h, 2w or YYYY-MM-DD)") from None


def thread_key(refs: str, irt: str, msgid: str) -> str:
    """Root message-id of a conversation."""
    ids = re.findall(r"<[^>]+>", refs or "") or re.findall(r"<[^>]+>", irt or "")
    return ids[0] if ids else (msgid or "")


class Store:
    """Thin wrapper around the SQLite database."""

    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        try:
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(fr, subject, body)")
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False

    def close(self) -> None:
        self.db.close()

    def __del__(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass

    def q(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple | list = ()) -> sqlite3.Row | None:
        return self.db.execute(sql, args).fetchone()

    # folders
    def folder_state(self, acct: str, name: str) -> tuple[int, int] | None:
        r = self.one("SELECT uidvalidity, last_uid FROM folders WHERE acct=? AND name=?", (acct, name))
        return (r[0], r[1]) if r else None

    def set_folder_state(self, acct: str, name: str, uidvalidity: int, last_uid: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO folders VALUES(?,?,?,?)", (acct, name, uidvalidity, last_uid))

    def drop_folder(self, acct: str, name: str) -> None:
        ids = [r[0] for r in self.q("SELECT id FROM msgs WHERE acct=? AND folder=?", (acct, name))]
        self.delete_msgs(ids)
        self.db.execute("DELETE FROM folders WHERE acct=? AND name=?", (acct, name))

    # messages
    def add_msg(self, rec: dict) -> int | None:
        """Insert a parsed message; returns rowid or None if already cached."""
        rec = dict(rec, thread=thread_key(rec.get("refs", ""), rec.get("irt", ""), rec.get("msgid", "")),
                   atts=json.dumps(rec.get("atts") or []))
        cur = self.db.execute(f"INSERT OR IGNORE INTO msgs({','.join(FIELDS)}) VALUES({','.join('?' * len(FIELDS))})",
                              [rec.get(f) for f in FIELDS])
        if not cur.rowcount:
            return None
        if self.fts:
            self.db.execute("INSERT INTO fts(rowid, fr, subject, body) VALUES(?,?,?,?)",
                            (cur.lastrowid, f"{rec['from_name']} {rec['from_addr']}", rec["subject"], rec["body"]))
        return cur.lastrowid

    def delete_msgs(self, ids: list[int]) -> None:
        for i in ids:
            self.db.execute("DELETE FROM msgs WHERE id=?", (i,))
            self.db.execute("DELETE FROM seen WHERE msg=?", (i,))
            if self.fts:
                self.db.execute("DELETE FROM fts WHERE rowid=?", (i,))

    def uids(self, acct: str, folder: str) -> dict[int, int]:
        """uid -> rowid for one folder."""
        return {r[0]: r[1] for r in self.q("SELECT uid, id FROM msgs WHERE acct=? AND folder=?", (acct, folder))}

    def set_flags(self, rowid: int, flags: str) -> None:
        self.db.execute("UPDATE msgs SET flags=?, unread=? WHERE id=?", (flags, int("\\Seen" not in flags), rowid))

    def get(self, rowid: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM msgs WHERE id=?", (rowid,))

    @staticmethod
    def _filters(acct=None, folder=None, sender=None, since=None, unread=False) -> tuple[str, list]:
        w, a = [], []
        if acct:
            w.append("m.acct=?"); a.append(acct)
        if folder:
            w.append("m.folder=?"); a.append(folder)
        if sender:
            w.append("(m.from_name LIKE ? OR m.from_addr LIKE ?)"); a += [f"%{sender}%"] * 2
        if since:
            w.append("m.date>=?"); a.append(parse_since(since))
        if unread:
            w.append("m.unread=1")
        return " AND ".join(w) or "1", a

    def list(self, limit: int = 20, **f) -> list[sqlite3.Row]:
        w, a = self._filters(**f)
        return self.q(f"SELECT {LIST_COLS} FROM msgs m WHERE {w} ORDER BY m.date DESC, m.id DESC LIMIT ?", a + [limit])

    def search(self, query: str, limit: int = 20, **f) -> list[sqlite3.Row]:
        w, a = self._filters(**f)
        cols = ", ".join("m." + c.strip() for c in LIST_COLS.split(","))
        if self.fts:
            terms = " ".join('"' + t.replace('"', '""') + '"' for t in query.split())
            return self.q(f"SELECT {cols} FROM fts JOIN msgs m ON m.id=fts.rowid WHERE fts MATCH ? AND {w} "
                          "ORDER BY m.date DESC LIMIT ?", [terms] + a + [limit])
        tw, ta = [], []
        for t in query.split():
            tw.append("(m.from_name LIKE ? OR m.from_addr LIKE ? OR m.subject LIKE ? OR m.body LIKE ?)")
            ta += [f"%{t}%"] * 4
        return self.q(f"SELECT {cols} FROM msgs m WHERE {' AND '.join(tw) or '1'} AND {w} "
                      "ORDER BY m.date DESC LIMIT ?", ta + a + [limit])

    def thread(self, rowid: int) -> list[sqlite3.Row]:
        """All cached messages of the conversation, oldest first, deduplicated by Message-ID."""
        m = self.get(rowid)
        if not m:
            return []
        rows = self.q("SELECT * FROM msgs WHERE thread=? OR msgid=? OR (thread='' AND id=?) ORDER BY date, id",
                      (m["thread"], m["thread"], rowid))
        seen, out = set(), []
        for r in rows:
            if r["msgid"] and r["msgid"] in seen:
                continue
            seen.add(r["msgid"])
            out.append(r)
        return out

    def stats(self) -> list[sqlite3.Row]:
        return self.q("SELECT acct, folder, COUNT(*) n, SUM(unread) u, MAX(date) newest FROM msgs "
                      "GROUP BY acct, folder ORDER BY acct, folder")

    # watchers
    def watcher_new(self, name: str, acct=None, folder=None, backlog: bool = False) -> list[sqlite3.Row]:
        """Unseen messages for a watcher; marks them seen. A new watcher starts at 'now' unless backlog."""
        fresh = not self.one("SELECT 1 FROM watchers WHERE name=?", (name,))
        w, a = self._filters(acct=acct, folder=folder)
        rows = self.q(f"SELECT {LIST_COLS}, body FROM msgs m WHERE {w} AND NOT EXISTS "
                      "(SELECT 1 FROM seen s WHERE s.watcher=? AND s.msg=m.id) ORDER BY m.date, m.id", a + [name])
        self.db.execute("BEGIN")
        self.db.execute("INSERT OR IGNORE INTO watchers VALUES(?,?)", (name, int(time.time())))
        self.db.executemany("INSERT OR IGNORE INTO seen VALUES(?,?)", [(name, r["id"]) for r in rows])
        self.db.execute("COMMIT")
        return [] if fresh and not backlog else rows

    # drafts
    def add_draft(self, **d) -> int:
        cols = list(d)
        cur = self.db.execute(f"INSERT INTO drafts({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                              [d[c] for c in cols])
        return cur.lastrowid

    def draft(self, rowid: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM drafts WHERE id=?", (rowid,))

    def pending(self) -> list[sqlite3.Row]:
        self.expire()
        return self.q("SELECT * FROM drafts WHERE status='pending' ORDER BY id")

    def claim(self, rowid: int, frm: str, to: str) -> bool:
        """Atomically move a draft from one status to another."""
        return self.db.execute("UPDATE drafts SET status=? WHERE id=? AND status=?", (to, rowid, frm)).rowcount == 1

    def set_status(self, rowid: int, status: str, error: str | None = None) -> None:
        self.db.execute("UPDATE drafts SET status=?, error=? WHERE id=?", (status, error, rowid))

    def expire(self) -> list[int]:
        ids = [r[0] for r in self.q("SELECT id FROM drafts WHERE status='pending' AND expires<?", (int(time.time()),))]
        for i in ids:
            if self.claim(i, "pending", "expired"):
                self.audit(i, "expired", "system")
        return ids

    def audit(self, draft: int, action: str, via: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO audit(ts, draft, action, via, detail) VALUES(?,?,?,?,?)",
                        (int(time.time()), draft, action, via, detail))

    def log(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.q("SELECT a.*, d.to_addr, d.subject FROM audit a LEFT JOIN drafts d ON d.id=a.draft "
                      "ORDER BY a.id DESC LIMIT ?", (limit,))

    # misc
    def kv_get(self, k: str, default: str | None = None) -> str | None:
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return r[0] if r else default

    def kv_set(self, k: str, v: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, v))
