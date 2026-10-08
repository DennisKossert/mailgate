"""Background sync shared by `mg ui` and `mg daemon --sync`: interval sync, IMAP IDLE, rules, reminders."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import imapops, imapsync, sortrules
from .config import Config
from .store import Store, b36


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, file=sys.stderr, flush=True)


def process_new(cfg: Config, store: Store, reg=None, unattended: bool = False,
                log: Callable[[str], None] = log) -> list[str]:
    """After a sync: events + on_message_synced for new mail (every sync, local only); with unattended=True
    (mg ui / mg daemon --sync) also the sorting rules and on_new_mail hooks. Returns rule log lines."""
    rows = sortrules.claim_new(store, "synced_id")
    for r in rows:
        store.emit("new_mail", {"id": b36(r["id"]), "account": r["acct"], "folder": r["folder"],
                                "from": r["from_addr"], "from_name": r["from_name"], "subject": r["subject"],
                                "date": r["date"], "message_id": r["msgid"]})
    if reg is not None and rows:
        reg.synced(rows)
    if not unattended:
        return []
    new = sortrules.claim_new(store, "rules_last_id")
    lines = sortrules.apply(cfg, store, new, log, reg) if new and cfg.sort_rules else []
    if reg is not None and new and reg.has("new_mail"):
        reg.new_mail([r for r in (store.get(x["id"]) for x in new) if r])
    return lines


class Syncer:
    def __init__(self, cfg: Config, db: Path, minutes: float, publish: Callable[[dict], None] = lambda ev: None,
                 reg=None):
        self.cfg, self.db, self.minutes, self.publish = cfg, db, minutes, publish
        if reg is None:
            from . import plugin
            reg = plugin.load(cfg, db, publish)
        self.reg = reg
        self.sync_lock = threading.Lock()
        self.wake = threading.Event()
        self.last_sync = 0
        self.last_error = ""

    def store(self) -> Store:
        return Store(self.db)

    def sync(self) -> int:
        """Sync all accounts, apply sorting rules to new mail, check reminders. Returns new message count."""
        with self.sync_lock:
            store, total, errors = self.store(), 0, []
            for acct in self.cfg.accounts.values():
                try:
                    total += imapsync.sync_account(store, acct, None, self.cfg.max_raw_bytes, self.cfg.initial_days,
                                                   log=lambda s: errors.append(s) if " error: " in s else None)
                except Exception as e:
                    errors.append(f"{acct.name}: {e}")
            try:  # [[rules]] come from the config file (the user), so they run even in read-only mode
                for line in process_new(self.cfg, store, self.reg, True, lambda s: errors.append(f"rules: {s}")):
                    log(f"rules: {line}")
            except Exception as e:
                errors.append(f"after sync: {type(e).__name__}: {e}")
            self.last_sync, self.last_error = int(time.time()), "; ".join(errors)[:300]
            for e in errors:
                log(f"sync: {e}")
            self.publish({"t": "sync", "new": total, "err": self.last_error})
            store.emit("sync", {"new": total, "error": self.last_error})
            return total

    def tick(self) -> None:
        try:
            self.reg.tick()
        except Exception as e:  # a plugin must never stop the sync loop
            log(f"plugin tick: {e}")

    def sync_loop(self, stop: threading.Event) -> None:
        """Sync every `minutes` (or when woken by IDLE / the UI); plugin ticks run every minute."""
        next_sync = 0.0
        while not stop.is_set():
            if self.wake.is_set() or time.time() >= next_sync:
                self.wake.clear()
                try:
                    self.sync()
                except Exception as e:
                    log(f"sync failed: {type(e).__name__}: {e}")
                next_sync = time.time() + self.minutes * 60
            self.tick()
            self.wake.wait(min(60, max(1, next_sync - time.time())))

    def idle_loop(self, acct, stop: threading.Event) -> None:
        delay = 5.0
        while not stop.is_set():
            started = time.time()
            try:
                imapops.idle(acct, self.wake.set, stop)
                if time.time() - started < 5:
                    return  # no IDLE support
            except Exception as e:
                log(f"idle {acct.name}: {e}")
            stop.wait(delay)
            delay = 5.0 if time.time() - started > 120 else min(delay * 2, 300)

    def threads(self, stop: threading.Event, idle: bool = True) -> list[threading.Thread]:
        ts = [threading.Thread(target=self.sync_loop, args=(stop,), daemon=True)]
        if idle:
            ts += [threading.Thread(target=self.idle_loop, args=(a, stop), daemon=True)
                   for a in self.cfg.accounts.values() if "INBOX" in a.folders]
        return ts
