"""`mg daemon`: ntfy reply listener and localhost approval web UI."""
from __future__ import annotations

import hmac
import html
import json
import secrets
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .approve import ApprovalError, handle_command, process_due, reject_draft, send_draft
from .compose import preview
from .config import LOOPBACK, Config
from .store import Store, draft_ref, parse_id


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, file=sys.stderr, flush=True)


# ---- ntfy -----------------------------------------------------------------

def ntfy_stream(cfg: Config, store: Store, stop: threading.Event, timeout: float = 90) -> None:
    """Consume one ntfy JSON stream connection until it ends or stop is set."""
    n = cfg.ntfy
    assert n
    since = store.kv_get("ntfy_since") or str(int(time.time() - cfg.expiry_hours * 3600))
    url = f"{n.server}/{n.reply_topic}/json?since={urllib.parse.quote(since)}"
    req = urllib.request.Request(url, headers=n.auth_header())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for line in r:
            if stop.is_set():
                return
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event") != "message":
                continue
            res = handle_command(cfg, store, ev.get("message", ""), via="ntfy")
            log(f"ntfy: {res}")
            if ev.get("id"):
                store.kv_set("ntfy_since", ev["id"])


def ntfy_loop(cfg: Config, db: Path, stop: threading.Event) -> None:
    """Keep the ntfy subscription alive, reconnecting with exponential backoff."""
    store = Store(db)
    delay = 1.0
    while not stop.is_set():
        started = time.time()
        try:
            ntfy_stream(cfg, store, stop)
        except Exception as e:
            log(f"ntfy: connection error: {e}")
        if time.time() - started > 60:
            delay = 1.0
        stop.wait(delay)
        delay = min(delay * 2, 120)


# ---- web UI ---------------------------------------------------------------

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>mailgate queue</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font:15px system-ui,sans-serif;max-width:52rem;margin:1rem auto;padding:0 1rem}}
pre{{white-space:pre-wrap;background:#f4f4f4;padding:.8rem;border-radius:6px}}
.d{{border-bottom:1px solid #ccc;padding:1rem 0}} button{{font-size:1rem;padding:.4rem 1rem;margin-right:.5rem}}
.msg{{background:#eef;padding:.5rem}}</style></head><body><h1>mailgate: pending drafts</h1>{msg}{items}
</body></html>"""
ITEM = """<div class="d"><b>{ref}</b> account {acct}, {exp}<pre>{text}</pre>
<form method="post" action="/act"><input type="hidden" name="csrf" value="{csrf}">
<input type="hidden" name="id" value="{ref}">{buttons}</form></div>"""
BUTTON = '<button name="do" value="{v}">{label}</button>'


def make_handler(cfg: Config, db: Path, csrf: str, allowed_hosts: set[str]):
    labels = ((cfg.ntfy.approve_label, cfg.ntfy.reject_label, cfg.ntfy.stop_label) if cfg.ntfy
              else ("Send", "Discard", "Stop"))

    def buttons(d) -> str:
        if d["status"] == "scheduled":
            return BUTTON.format(v="discard", label=html.escape(labels[2]))
        return BUTTON.format(v="send", label=html.escape(labels[0])) + BUTTON.format(v="discard", label=html.escape(labels[1]))

    def when(d) -> str:
        if d["status"] == "scheduled":
            return "sends automatically at " + time.strftime("%m-%d %H:%M:%S", time.localtime(d["send_at"]))
        return "expires " + time.strftime("%m-%d %H:%M", time.localtime(d["expires"]))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            pass

        def _reply(self, code: int, body: str, extra: dict | None = None) -> None:
            data = body.encode()
            self.send_response(code)
            for k, v in {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(data)),
                         "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
                         "X-Frame-Options": "DENY", "Cache-Control": "no-store", **(extra or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts

        def do_GET(self) -> None:
            if not self._host_ok():
                return self._reply(403, "bad host")
            if urllib.parse.urlsplit(self.path).path != "/":
                return self._reply(404, "not found")
            msg = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("m", [""])[0]
            store = Store(db)
            items = "".join(ITEM.format(
                ref=draft_ref(d["id"]), acct=html.escape(d["acct"]), csrf=csrf,
                exp=when(d), text=html.escape(preview(bytes(d["mime"]))), buttons=buttons(d))
                for d in store.pending()) or "<p>Nothing pending.</p>"
            self._reply(200, PAGE.format(msg=f'<p class="msg">{html.escape(msg)}</p>' if msg else "", items=items))

        def do_POST(self) -> None:
            origin = self.headers.get("Origin")
            if not self._host_ok() or (origin and origin.split("//", 1)[-1] not in allowed_hosts):
                return self._reply(403, "bad host or origin")
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode(errors="replace"))
            if self.path != "/act" or not hmac.compare_digest(form.get("csrf", [""])[0], csrf):
                return self._reply(403, "bad csrf token")
            store = Store(db)
            try:
                did = parse_id(form.get("id", [""])[0], draft=True)
                if form.get("do", [""])[0] == "send":
                    res = send_draft(cfg, store, did, via="web")
                else:
                    res = reject_draft(store, did, via="web")
            except (ApprovalError, ValueError) as e:
                res = str(e)
            log(f"web: {res}")
            self._reply(303, "", {"Location": "/?m=" + urllib.parse.quote(res)})

    return Handler


def web_server(cfg: Config, db: Path, bind: str) -> ThreadingHTTPServer:
    """Create (not start) the approval web server; only loopback addresses are allowed."""
    host, _, port = bind.rpartition(":")
    host = host.strip("[]") or "127.0.0.1"
    if host not in LOOPBACK:
        raise ValueError("web UI may only listen on 127.0.0.1, ::1 or localhost")
    srv = ThreadingHTTPServer((host, int(port)), lambda *a: None)  # placeholder handler
    real_port = srv.server_address[1]
    hosts = {f"{h}:{real_port}" for h in ("127.0.0.1", "localhost", "[::1]")}
    srv.RequestHandlerClass = make_handler(cfg, db, secrets.token_urlsafe(24), hosts)
    return srv


# ---- main -----------------------------------------------------------------

def run(cfg: Config, db: Path, web: str | None = None, use_ntfy: bool = True, sync: bool = False) -> None:
    """Run until SIGINT/SIGTERM. sync=True adds background sync, IMAP IDLE, sorting rules and reminders."""
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    threads = []
    srv = None
    from .config import insecure_paths
    for w in insecure_paths():
        log(f"warning: {w}")
    if web:
        srv = web_server(cfg, db, web)
        h, p = srv.server_address[:2]
        log(f"web UI on http://{h}:{p}/")
        threads.append(threading.Thread(target=srv.serve_forever, daemon=True))
    if use_ntfy and cfg.ntfy:
        log(f"listening on ntfy reply topic at {cfg.ntfy.server}")
        threads.append(threading.Thread(target=ntfy_loop, args=(cfg, db, stop), daemon=True))
    if sync:
        from .syncer import Syncer
        log(f"syncing every {cfg.sync_minutes:g} min (+ IMAP IDLE), {len(cfg.sort_rules)} sorting rules")
        threads += Syncer(cfg, db, cfg.sync_minutes).threads(stop)
    if not threads:
        raise SystemExit("nothing to do: configure [approval.ntfy], pass --sync or --web 127.0.0.1:8765")
    for t in threads:
        t.start()
    store = Store(db)
    try:
        while not stop.is_set():
            for i in store.expire():
                log(f"{draft_ref(i)} expired")
            for res in process_due(cfg, store):
                log(f"auto: {res}")
            stop.wait(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if srv:
            srv.shutdown()
