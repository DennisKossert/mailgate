"""Incremental IMAP sync (UIDVALIDITY + UID) into the local store, plus APPEND."""
from __future__ import annotations

import base64
import imaplib
import re
import ssl
import time
from datetime import datetime, timedelta
from typing import Callable

from .clean import parse_message
from .config import Account
from .store import Store

BATCH = 50


class SyncError(Exception):
    """IMAP command failed."""


def utf7(name: str) -> str:
    """Encode a folder name as IMAP modified UTF-7 (RFC 3501)."""
    out, buf = [], []

    def flush() -> None:
        if buf:
            b = base64.b64encode("".join(buf).encode("utf-16-be")).decode().rstrip("=")
            out.append("&" + b.replace("/", ",") + "-")
            buf.clear()
    for ch in name:
        if 0x20 <= ord(ch) <= 0x7e:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def quote(name: str) -> str:
    n = utf7(name)
    return '"' + n.replace("\\", "\\\\").replace('"', '\\"') + '"'


def connect(acct: Account) -> imaplib.IMAP4:
    """Open an authenticated IMAP connection."""
    s = acct.imap
    if s.security == "ssl":
        conn = imaplib.IMAP4_SSL(s.host, s.port, ssl_context=ssl.create_default_context(), timeout=60)
    else:
        conn = imaplib.IMAP4(s.host, s.port, timeout=60)
        if s.security == "starttls":
            conn.starttls(ssl.create_default_context())
    conn.login(acct.user, acct.password())
    return conn


def _ok(res: tuple, what: str) -> list:
    typ, data = res
    if typ != "OK":
        raise SyncError(f"{what}: {data!r}")
    return data


def _fetch_items(data: list) -> list[tuple[bytes, bytes | None]]:
    """Group imaplib FETCH output into (metadata, literal) pairs."""
    items: list[tuple[bytes, bytes | None]] = []
    i = 0
    while i < len(data):
        d = data[i]
        if isinstance(d, tuple):
            meta, lit = d[0], d[1]
            if i + 1 < len(data) and isinstance(data[i + 1], bytes):
                meta += data[i + 1]
                i += 1
            items.append((meta, lit))
        elif isinstance(d, bytes) and d.strip() not in (b"", b")"):
            items.append((d, None))
        i += 1
    return items


def _meta(meta: bytes) -> tuple[int, str, int]:
    uid = re.search(rb"UID (\d+)", meta)
    flags = re.search(rb"FLAGS \(([^)]*)\)", meta)
    size = re.search(rb"RFC822\.SIZE (\d+)", meta)
    return (int(uid.group(1)) if uid else 0, flags.group(1).decode() if flags else "",
            int(size.group(1)) if size else 0)


def _chunks(seq: list[int], n: int = BATCH):
    for i in range(0, len(seq), n):
        yield ",".join(map(str, seq[i:i + n]))


def sync_folder(conn: imaplib.IMAP4, store: Store, acct: Account, folder: str,
                max_raw: int, initial_days: int = 0) -> int:
    """Sync one folder read-only. Returns number of new messages."""
    _ok(conn.select(quote(folder), readonly=True), f"select {folder}")
    uv = conn.response("UIDVALIDITY")[1]
    uidvalidity = int(uv[0]) if uv and uv[0] else 0
    state = store.folder_state(acct.name, folder)
    if state and state[0] != uidvalidity:
        store.drop_folder(acct.name, folder)
        state = None
    last = state[1] if state else 0

    known = store.uids(acct.name, folder)
    if known:  # refresh flags, drop messages deleted on the server
        present = set()
        for meta, _ in _fetch_items(_ok(conn.uid("FETCH", f"1:{last}", "(UID FLAGS)"), "fetch flags")):
            uid, flags, _ = _meta(meta)
            if uid in known:
                present.add(uid)
                store.set_flags(known[uid], flags)
        store.delete_msgs([rid for uid, rid in known.items() if uid not in present])

    if not state and initial_days:
        since = (datetime.now() - timedelta(days=initial_days)).strftime("%d-%b-%Y")
        data = _ok(conn.uid("SEARCH", None, "SINCE", since), "search")
    else:
        data = _ok(conn.uid("SEARCH", None, "UID", f"{last + 1}:*"), "search")
    uids = sorted(u for u in map(int, (data[0] or b"").split()) if u > last)
    new = 0
    for chunk in _chunks(uids):
        data = _ok(conn.uid("FETCH", chunk, "(UID FLAGS RFC822.SIZE BODY.PEEK[])"), "fetch")
        store.db.execute("BEGIN")
        for meta, raw in _fetch_items(data):
            uid, flags, size = _meta(meta)
            if not uid or raw is None:
                continue
            rec = parse_message(raw)
            rec.update(acct=acct.name, folder=folder, uid=uid, uidvalidity=uidvalidity, flags=flags,
                       unread=int("\\Seen" not in flags), size=size or len(raw),
                       raw=raw if len(raw) <= max_raw else None, date=rec["date"] or int(time.time()))
            if store.add_msg(rec):
                new += 1
            last = max(last, uid)
        store.set_folder_state(acct.name, folder, uidvalidity, last)
        store.db.execute("COMMIT")
    store.set_folder_state(acct.name, folder, uidvalidity, max([last] + uids))
    return new


def sync_account(store: Store, acct: Account, folders: list[str] | None = None, max_raw: int = 5_000_000,
                 initial_days: int = 0, log: Callable[[str], None] = print) -> int:
    """Sync the given (or configured) folders of one account."""
    conn = connect(acct)
    total = 0
    try:
        for f in folders or acct.folders:
            try:
                n = sync_folder(conn, store, acct, f, max_raw, initial_days)
            except (SyncError, imaplib.IMAP4.error) as e:
                if store.db.in_transaction:
                    store.db.execute("ROLLBACK")
                log(f"{acct.name}/{f} error: {e}")
                continue
            total += n
            log(f"{acct.name}/{f} +{n}")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return total


def fetch_raw(acct: Account, folder: str, uid: int) -> bytes:
    """Fetch one raw message (used when it was too big to cache)."""
    conn = connect(acct)
    try:
        _ok(conn.select(quote(folder), readonly=True), f"select {folder}")
        for meta, raw in _fetch_items(_ok(conn.uid("FETCH", str(uid), "(UID BODY.PEEK[])"), "fetch")):
            if raw is not None:
                return raw
        raise SyncError(f"uid {uid} not found in {folder}")
    finally:
        conn.logout()


def append(acct: Account, folder: str, mime: bytes) -> None:
    """Store a copy of a sent message, flagged \\Seen."""
    conn = connect(acct)
    try:
        _ok(conn.append(quote(folder), r"(\Seen)", imaplib.Time2Internaldate(time.time()), mime), "append")
    finally:
        conn.logout()


def list_folders(conn: imaplib.IMAP4) -> list[str]:
    """Raw (encoded) folder names from LIST."""
    out = []
    for line in _ok(conn.list(), "list"):
        if isinstance(line, bytes) and (m := re.match(rb'\(([^)]*)\) (?:"[^"]*"|NIL) (.+)$', line.strip())):
            name = m.group(2).decode(errors="replace")
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            out.append(name)
    return out
