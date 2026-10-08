"""Explicit write operations on the IMAP server: flags, move, archive, trash, plus IDLE.

Only `mg ui`, `mg mark`, `mg move` and `mg rules apply` use this module. `mg sync` stays read-only.
Nothing here expunges a message that was not just copied elsewhere.
"""
from __future__ import annotations

import base64
import imaplib
import re
import select
import threading
from collections import defaultdict
from typing import Callable

from .config import Account
from .imapsync import SyncError, _ok, connect, quote
from .store import Store

FLAGS = {"read": ("+", "\\Seen"), "unread": ("-", "\\Seen"), "flag": ("+", "\\Flagged"),
         "unflag": ("-", "\\Flagged"), "answered": ("+", "\\Answered")}


def special_folders(conn: imaplib.IMAP4) -> dict[str, str]:
    """{'\\Trash': name, '\\Archive': name, ...} from LIST special-use attributes (RFC 6154)."""
    out = {}
    for line in _ok(conn.list(), "list"):
        if isinstance(line, bytes) and (m := re.match(rb'\(([^)]*)\) (?:"[^"]*"|NIL) (.+)$', line.strip())):
            name = m.group(2).decode(errors="replace").strip('"')
            for attr in m.group(1).decode().split():
                if attr in ("\\Trash", "\\Archive", "\\Sent", "\\Junk", "\\Drafts"):
                    out.setdefault(attr, name)
    return out


def target_folder(conn: imaplib.IMAP4, acct: Account, kind: str) -> str:
    """Configured archive/trash folder, else the special-use folder, else 'Archive'/'Trash'."""
    configured = acct.archive_folder if kind == "archive" else acct.trash_folder
    if configured:
        return configured
    found = special_folders(conn).get("\\Archive" if kind == "archive" else "\\Trash")
    return _decode_name(found) if found else kind.capitalize()


def _decode_name(name: str) -> str:
    """Modified UTF-7 folder name back to str (inverse of imapsync.utf7)."""
    def dec(m: re.Match) -> str:
        if m.group(1) == "":
            return "&"
        b = m.group(1).replace(",", "/")
        return base64.b64decode(b + "=" * (-len(b) % 4)).decode("utf-16-be")
    return re.sub(r"&([^-]*)-", dec, name)


def _caps(conn: imaplib.IMAP4) -> set[str]:
    return {c.upper() for c in conn.capabilities}


def _group(store: Store, ids: list[int]) -> dict[tuple[str, str], list]:
    """(acct, folder) -> cached rows, so each folder is selected once."""
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for i in ids:
        r = store.get(i)
        if not r:
            raise ValueError(f"no message {i}")
        groups[(r["acct"], r["folder"])].append(r)
    return groups


def set_flag(store: Store, accounts: dict[str, Account], ids: list[int], op: str) -> int:
    """op: read | unread | flag | unflag | answered. Updates server and cache; returns count."""
    sign, flag = FLAGS[op]
    n = 0
    for (acct, folder), rows in _group(store, ids).items():
        conn = connect(accounts[acct])
        try:
            _ok(conn.select(quote(folder)), f"select {folder}")
            uids = ",".join(str(r["uid"]) for r in rows)
            _ok(conn.uid("STORE", uids, sign + "FLAGS.SILENT", f"({flag})"), "store")
        finally:
            _logout(conn)
        for r in rows:
            flags = set((r["flags"] or "").split())
            flags = flags | {flag} if sign == "+" else flags - {flag}
            store.set_flags(r["id"], " ".join(sorted(flags)))
            n += 1
    return n


def move(store: Store, accounts: dict[str, Account], ids: list[int], target: str | None = None,
         kind: str | None = None) -> tuple[int, str]:
    """Move messages to a folder (or kind='archive'/'trash'). Returns (count, target folder).

    Uses UID MOVE when the server has it, else COPY + \\Deleted + UID EXPUNGE of exactly these
    UIDs (UIDPLUS). Without UIDPLUS a plain EXPUNGE is only used when no other message in the
    folder carries \\Deleted; otherwise the originals stay flagged \\Deleted on the server.
    """
    n, dest = 0, target or ""
    for (acct, folder), rows in _group(store, ids).items():
        conn = connect(accounts[acct])
        try:
            dest = target or target_folder(conn, accounts[acct], kind or "archive")
            if dest == folder:
                continue  # already there; trash in Trash never expunges
            _ok(conn.select(quote(folder)), f"select {folder}")
            uids = ",".join(str(r["uid"]) for r in rows)
            caps = _caps(conn)
            if "MOVE" in caps:
                _ok(conn.uid("MOVE", uids, quote(dest)), f"move to {dest}")
            else:
                _ok(conn.uid("COPY", uids, quote(dest)), f"copy to {dest}")
                _ok(conn.uid("STORE", uids, "+FLAGS.SILENT", "(\\Deleted)"), "store \\Deleted")
                if "UIDPLUS" in caps:
                    _ok(conn.uid("EXPUNGE", uids), "uid expunge")
                else:
                    deleted = set((_ok(conn.uid("SEARCH", None, "DELETED"), "search")[0] or b"").split())
                    if deleted <= {str(r["uid"]).encode() for r in rows}:
                        _ok(conn.expunge(), "expunge")
        finally:
            _logout(conn)
        store.delete_msgs([r["id"] for r in rows])
        n += len(rows)
    return n, dest


def _logout(conn: imaplib.IMAP4) -> None:
    try:
        conn.logout()
    except Exception:
        pass


def server_folders(acct: Account) -> list[str]:
    """All folder names on the server, decoded, for move targets."""
    from .imapsync import list_folders
    conn = connect(acct)
    try:
        return sorted({_decode_name(f) for f in list_folders(conn)}, key=lambda f: (f != "INBOX", f.lower()))
    finally:
        _logout(conn)


def idle(acct: Account, on_change: Callable[[], None], stop: threading.Event, folder: str = "INBOX",
         renew: float = 25 * 60) -> None:
    """IMAP IDLE on one folder until stop is set; calls on_change for EXISTS/EXPUNGE/FETCH.

    Returns early (without error) if the server has no IDLE capability.
    """
    conn = connect(acct)
    try:
        if "IDLE" not in _caps(conn):
            return
        _ok(conn.select(quote(folder), readonly=True), f"examine {folder}")
        while not stop.is_set():
            tag = conn._new_tag()
            conn.send(tag + b" IDLE\r\n")
            if not conn.readline().startswith(b"+"):
                raise SyncError("IDLE refused")
            changed, waited = False, 0.0
            while not stop.is_set() and waited < renew and not changed:
                ready = conn.sock.pending() if hasattr(conn.sock, "pending") else 0
                if not ready and not select.select([conn.sock], [], [], 1.0)[0]:
                    waited += 1.0
                    continue
                line = conn.readline()
                if not line:
                    raise SyncError("connection closed during IDLE")
                changed = bool(re.match(rb"\* \d+ (EXISTS|EXPUNGE|FETCH)", line))
            conn.send(b"DONE\r\n")
            while not (line := conn.readline()).startswith(tag):
                if not line:
                    raise SyncError("connection closed after IDLE")
            if changed:
                on_change()
    finally:
        _logout(conn)
