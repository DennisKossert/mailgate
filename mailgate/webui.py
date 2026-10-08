"""`mg ui`: optional local web mail client for humans.

Serves a single-page app from mailgate/static on a loopback address. Reading works without
login only when no UI passphrase is set (then the UI is read-only). With a passphrase, every
API call needs a session cookie, and every write also needs the session's CSRF token.
Sending from here is a human action and skips the approval queue, so the passphrase is
what keeps an agent on the same machine from using this server to send mail.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, approve, compose, imapops, imapsync, sortrules
from .clean import attachment_parts
from .config import LOOPBACK, Config, config_path
from .store import Store, b36, draft_ref, parse_id

STATIC = Path(__file__).parent / "static"
STATIC_TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}
SESSION_IDLE = 12 * 3600
MAX_POST = 40_000_000  # compose uploads are base64 JSON
COOKIE = "mg_ui"
APP_CSP = ("default-src 'self'; img-src 'self' data:; frame-src 'self'; object-src 'none'; base-uri 'none'; "
           "form-action 'self'; frame-ancestors 'none'")


def log(msg: str) -> None:
    import sys
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, file=sys.stderr, flush=True)


# ---- passphrase -------------------------------------------------------------------

def pass_path() -> Path:
    return config_path().parent / "ui-passphrase"


def _scrypt(pw: str, salt: bytes, n: int = 2 ** 15) -> bytes:
    return hashlib.scrypt(pw.encode(), salt=salt, n=n, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def set_passphrase(pw: str, path: Path | None = None) -> None:
    """Store only a salted scrypt hash, mode 0600."""
    if len(pw) < 8:
        raise ValueError("passphrase must have at least 8 characters")
    path = path or pass_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = os.urandom(16)
    data = json.dumps({"kdf": "scrypt", "n": 2 ** 15, "r": 8, "p": 1, "salt": salt.hex(),
                       "hash": _scrypt(pw, salt).hex()})
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data + "\n")
    os.chmod(path, 0o600)


def check_passphrase(pw: str, path: Path | None = None) -> bool:
    try:
        d = json.loads((path or pass_path()).read_text())
        return hmac.compare_digest(_scrypt(pw, bytes.fromhex(d["salt"]), int(d["n"])).hex(), d["hash"])
    except (OSError, ValueError, KeyError):
        return False


# ---- HTML mail sanitising -------------------------------------------------------------

DROP_ALL = {"script", "template", "iframe", "object", "embed", "applet", "frame", "frameset", "title",
            "noscript", "svg", "math"}
DROP_TAG = {"html", "head", "body", "form", "base", "meta", "link"}
URL_ATTRS = {"href", "src", "background", "action", "formaction", "poster", "cite", "longdesc", "xlink:href"}
REMOTE_CSS = re.compile(r"url\(\s*['\"]?\s*(?:https?:)?//", re.I)


class _Sanitizer(HTMLParser):
    def __init__(self, cids: dict[str, str], images: bool):
        super().__init__(convert_charrefs=True)
        self.cids, self.images = cids, images
        self.out: list[str] = []
        self.skip = 0
        self.blocked = 0
        self.in_style = False

    def _attrs(self, tag: str, attrs: list) -> str:
        out = []
        for k, v in attrs:
            k, v = k.lower(), v or ""
            if k.startswith("on") or k in ("srcset", "ping", "formaction", "action", "http-equiv"):
                continue
            if k in URL_ATTRS:
                low = v.strip().lower()
                if low.startswith("cid:"):
                    v = self.cids.get(v.strip()[4:].strip("<>"), "")
                elif low.startswith(("http://", "https://", "//")) and k != "href":
                    if not self.images:
                        self.blocked += 1
                        continue
                elif k == "href" and not low.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                elif k != "href" and not low.startswith("data:image/"):
                    continue
            if k == "style" and REMOTE_CSS.search(v) and not self.images:
                self.blocked += 1
            out.append(f' {k}="{html.escape(v)}"')
        if tag == "a":
            out.append(' target="_blank" rel="noopener noreferrer"')
        return "".join(out)

    def handle_starttag(self, tag: str, attrs: list, close: bool = False) -> None:
        if tag in DROP_ALL:
            self.skip += 0 if close else 1
            return
        if self.skip or tag in DROP_TAG:
            return
        self.in_style = tag == "style" and not close
        self.out.append(f"<{tag}{self._attrs(tag, attrs)}{' /' if close else ''}>")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        self.handle_starttag(tag, attrs, close=True)

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_ALL:
            self.skip = max(0, self.skip - 1)
        elif not self.skip and tag not in DROP_TAG:
            self.in_style = False
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.in_style:
            if REMOTE_CSS.search(data) and not self.images:
                self.blocked += 1
            self.out.append(data.replace("</", "<\\/"))
        else:
            self.out.append(html.escape(data, quote=False))


BASE_CSS = ("html{color-scheme:light}body{margin:0;padding:16px;font:15px/1.5 system-ui,sans-serif;"
            "color:#1c1c1c;background:#fff;overflow-wrap:anywhere}img{max-width:100%;height:auto}"
            "pre{white-space:pre-wrap}")


def sanitize(markup: str, cids: dict[str, str] | None = None, images: bool = False) -> tuple[str, int]:
    """Rewrite mail HTML into a standalone document without scripts, forms or remote loads.

    Returns (document, number of blocked remote images). The CSP from mail_csp() is the second
    line of defence; the iframe sandbox the third.
    """
    p = _Sanitizer(cids or {}, images)
    try:
        p.feed(markup)
        p.close()
    except Exception:
        pass
    doc = (f'<!doctype html><html><head><meta charset="utf-8"><base target="_blank">'
           f"<style>{BASE_CSS}</style></head><body>{''.join(p.out)}</body></html>")
    return doc, p.blocked


def mail_csp(images: bool) -> str:
    img = "data: cid:" + (" https: http:" if images else "")
    return (f"default-src 'none'; img-src {img}; style-src 'unsafe-inline'; font-src data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'self'; "
            "sandbox allow-popups allow-popups-to-escape-sandbox")


# ---- message helpers -------------------------------------------------------------------

def parse_raw(cfg: Config, row) -> EmailMessage:
    raw = row["raw"]
    if raw is None:
        raw = imapsync.fetch_raw(cfg.account(row["acct"]), row["folder"], row["uid"])
    return BytesParser(policy=policy.default).parsebytes(bytes(raw))


def html_body(msg: EmailMessage) -> str | None:
    part = msg.get_body(preferencelist=("html",))
    if part is None or part.get_content_type() != "text/html":
        return None
    try:
        return part.get_content()
    except Exception:
        return (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")


def cid_images(msg: EmailMessage) -> dict[str, str]:
    """Content-ID -> data: URI for inline images (from the cache, never fetched remotely)."""
    out = {}
    for part in msg.walk():
        cid = part.get("Content-ID")
        if cid and part.get_content_maintype() == "image":
            data = part.get_payload(decode=True) or b""
            if len(data) <= 5_000_000:
                out[cid.strip().strip("<>")] = f"data:{part.get_content_type()};base64,{base64.b64encode(data).decode()}"
    return out


def item(r) -> dict:
    flags = r["flags"] or ""
    return {"id": b36(r["id"]), "a": r["acct"], "f": r["folder"], "d": r["date"], "fr": r["from_name"],
            "e": r["from_addr"], "s": r["subject"], "p": " ".join((r["pv"] or "").split())[:140],
            "u": r["unread"], "fl": int("\\Flagged" in flags), "an": int("\\Answered" in flags),
            "at": int(bool(r["atts"]) and r["atts"] != "[]")}


# ---- live events ---------------------------------------------------------------------------

class Events:
    """Tiny pub/sub for Server-Sent Events."""

    def __init__(self):
        self.cond = threading.Condition()
        self.seq = 0
        self.items: list[tuple[int, dict]] = []

    def publish(self, ev: dict) -> None:
        with self.cond:
            self.seq += 1
            self.items = self.items[-50:] + [(self.seq, ev)]
            self.cond.notify_all()

    def wait(self, after: int, timeout: float) -> tuple[int, list[dict]]:
        with self.cond:
            self.cond.wait_for(lambda: self.seq > after, timeout)
            return self.seq, [e for s, e in self.items if s > after]


# ---- application state -----------------------------------------------------------------------

class App:
    def __init__(self, cfg: Config, db: Path, passfile: Path | None = None):
        self.cfg, self.db = cfg, db
        self.passfile = passfile or pass_path()
        self.sessions: dict[str, dict] = {}
        self.events = Events()
        self.login_lock = threading.Lock()
        self.sync_lock = threading.Lock()
        self.wake = threading.Event()
        self.folder_cache: dict[str, tuple[float, list[str]]] = {}
        self.last_sync = 0
        self.last_error = ""

    @property
    def readonly(self) -> bool:
        return not self.passfile.exists()

    def store(self) -> Store:
        return Store(self.db)

    # sessions
    def login(self, pw: str) -> tuple[str, str] | None:
        with self.login_lock:  # serialises guesses; each failure costs a second
            if self.readonly or not check_passphrase(pw, self.passfile):
                time.sleep(1)
                return None
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.sessions[hashlib.sha256(token.encode()).hexdigest()] = {"csrf": csrf, "last": time.time()}
        return token, csrf

    def session(self, cookie_header: str | None) -> dict | None:
        token = ""
        for part in (cookie_header or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                token = v
        s = self.sessions.get(hashlib.sha256(token.encode()).hexdigest()) if token else None
        if s and time.time() - s["last"] > SESSION_IDLE:
            self.logout(token)
            return None
        if s:
            s["last"] = time.time()
            s["token"] = token
        return s

    def logout(self, token: str) -> None:
        self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)

    # sync
    def sync(self) -> int:
        """Sync all accounts, apply sorting rules, publish an event. Returns new message count."""
        with self.sync_lock:
            store, total, errors = self.store(), 0, []
            for acct in self.cfg.accounts.values():
                try:
                    total += imapsync.sync_account(store, acct, None, self.cfg.max_raw_bytes, self.cfg.initial_days,
                                                   log=lambda s: errors.append(s) if " error: " in s else None)
                except Exception as e:
                    errors.append(f"{acct.name}: {e}")
            try:  # [[rules]] come from the config file (the user), so they run even in read-only mode
                for line in sortrules.apply_new(self.cfg, store):
                    log(f"rules: {line}")
            except Exception as e:
                errors.append(f"rules: {e}")
            self.last_sync, self.last_error = int(time.time()), "; ".join(errors)[:300]
            for e in errors:
                log(f"sync: {e}")
            self.events.publish({"t": "sync", "new": total, "err": self.last_error})
            return total

    def sync_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.sync()
            self.wake.wait(self.cfg.ui.sync_minutes * 60)
            self.wake.clear()

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

    def drafts_loop(self, stop: threading.Event) -> None:
        """Tell the page when agents queue or the approval daemon resolves drafts."""
        last = None
        store = self.store()
        while not stop.wait(3):
            sig = tuple(store.one("SELECT COUNT(*), COALESCE(MAX(id),0) FROM drafts "
                                  "WHERE status IN ('pending','scheduled')"))
            if sig != last:
                if last is not None:
                    self.events.publish({"t": "drafts", "n": sig[0]})
                last = sig

    def server_folders(self, acct: str) -> list[str]:
        hit = self.folder_cache.get(acct)
        if not hit or time.time() - hit[0] > 600:
            hit = (time.time(), imapops.server_folders(self.cfg.account(acct)))
            self.folder_cache[acct] = hit
        return hit[1]


# ---- API ------------------------------------------------------------------------------------

def state(app: App, sess: dict | None) -> dict:
    store = app.store()
    counts: dict[str, dict[str, list[int]]] = {}
    for r in store.stats():
        counts.setdefault(r["acct"], {})[r["folder"]] = [r["n"], r["u"] or 0]
    accounts = []
    for a in app.cfg.accounts.values():
        names = list(dict.fromkeys(a.folders + sorted(counts.get(a.name, {}))))
        accounts.append({"name": a.name, "email": a.email, "display": a.display_name, "signature": a.signature,
                         "folders": [{"name": f, "n": counts.get(a.name, {}).get(f, [0, 0])[0],
                                      "u": counts.get(a.name, {}).get(f, [0, 0])[1]} for f in names]})
    return {"version": __version__, "lang": app.cfg.ui.lang, "readonly": app.readonly, "auth": bool(sess),
            "need_login": not app.readonly and not sess, "csrf": sess["csrf"] if sess else "",
            "mark_read": app.cfg.ui.mark_read, "accounts": accounts if (sess or app.readonly) else [],
            "default": app.cfg.default_account, "drafts": len(store.pending()) if (sess or app.readonly) else 0,
            "last_sync": app.last_sync, "error": app.last_error}


def list_msgs(app: App, q: dict) -> dict:
    store = app.store()
    n = min(int(q.get("n", 50)), 200)
    before = None
    if q.get("before"):
        d, _, i = q["before"].partition(".")
        before = (int(d), parse_id(i))
    f = {"acct": q.get("acct") or None, "folder": q.get("folder") or None, "unread": q.get("unread") == "1",
         "before": before}
    extra = ", substr(m.body, 1, 200) AS pv, m.flags"
    rows = store.search(q["q"], limit=n, extra=extra, **f) if q.get("q") else store.list(limit=n, extra=extra, **f)
    items = [item(r) for r in rows]
    nxt = f"{rows[-1]['date']}.{b36(rows[-1]['id'])}" if len(rows) == n else ""
    return {"items": items, "next": nxt}


def message(app: App, rid: int) -> dict:
    store = app.store()
    r = store.get(rid)
    if not r:
        raise LookupError("no such message")
    msg = parse_raw(app.cfg, r)
    markup = html_body(msg)
    blocked = sanitize(markup)[1] if markup else 0
    atts = [{"n": i, "name": p.get_filename() or "unnamed", "size": len(p.get_payload(decode=True) or b""),
             "type": p.get_content_type()} for i, p in enumerate(attachment_parts(msg))]
    flags = r["flags"] or ""
    return {"id": b36(r["id"]), "acct": r["acct"], "folder": r["folder"], "from": r["from_name"],
            "addr": r["from_addr"], "to": r["to_addr"], "cc": r["cc"], "reply_to": r["reply_to"], "d": r["date"],
            "s": r["subject"], "u": r["unread"], "fl": int("\\Flagged" in flags), "text": r["body"],
            "full": r["full"], "html": bool(markup), "remote": blocked, "atts": atts,
            "thread": len(store.thread(rid)), "msgid": r["msgid"]}


FWD_RE = re.compile(r"^\s*(fwd?|wg|tr)\s*:", re.I)


def compose_fields(app: App, rid: int, mode: str) -> dict:
    """Prefill for reply / reply all / forward; the page adds the quote intro in its language."""
    r = app.store().get(rid)
    if not r:
        raise LookupError("no such message")
    acct = app.cfg.account(r["acct"])
    out = {"acct": acct.name, "to": "", "cc": "", "subject": r["subject"] or "", "from": r["from_name"],
           "addr": r["from_addr"], "d": r["date"], "orig_to": r["to_addr"], "orig_cc": r["cc"],
           "orig_subject": r["subject"], "text": r["full"] or "", "atts": len(json.loads(r["atts"] or "[]"))}
    if mode in ("reply", "all"):
        f = compose.reply_fields(acct, r, mode == "all")
        out.update(to=", ".join(f["to"]), cc=", ".join(f["cc"]), subject=f["subject"])
    elif not FWD_RE.match(out["subject"]):
        out["subject"] = f"Fwd: {out['subject']}".strip()
    return out


def thread(app: App, rid: int) -> list[dict]:
    return [{"id": b36(r["id"]), "from": r["from_name"], "addr": r["from_addr"], "d": r["date"], "s": r["subject"],
             "text": r["body"], "u": r["unread"]} for r in app.store().thread(rid)]


def drafts(app: App) -> list[dict]:
    return [{"id": draft_ref(d["id"]), "acct": d["acct"], "status": d["status"], "created": d["created"],
             "expires": d["expires"], "send_at": d["send_at"], "to": d["to_addr"], "s": d["subject"],
             "preview": compose.preview(bytes(d["mime"]))} for d in app.store().pending()]


def act(app: App, body: dict) -> dict:
    ids = [parse_id(i) for i in body.get("ids") or []]
    op = body.get("op")
    if not ids:
        raise ValueError("no messages")
    store = app.store()
    if op in imapops.FLAGS:
        n = imapops.set_flag(store, app.cfg.accounts, ids, op)
        return {"ok": f"{n} updated"}
    if op in ("archive", "trash", "move"):
        if op == "move" and not body.get("folder"):
            raise ValueError("no target folder")
        n, dest = imapops.move(store, app.cfg.accounts, ids, target=body.get("folder") if op == "move" else None,
                               kind=op if op != "move" else None)
        return {"ok": f"{n} moved to {dest}", "folder": dest}
    raise ValueError(f"unknown action {op!r}")


def send(app: App, body: dict) -> dict:
    """Human send from the compose form."""
    cfg, store = app.cfg, app.store()
    acct = cfg.account(body.get("acct") or None)
    mode, ref = body.get("mode", "new"), body.get("ref")
    orig = store.get(parse_id(ref)) if ref else None
    irt = refs = ""
    files = [(Path(a["name"]).name or "attachment", base64.b64decode(a["data"])) for a in body.get("atts") or []]
    if mode == "reply" and orig:
        irt, refs = orig["msgid"] or "", orig["refs"] or ""
    if mode == "forward" and orig and body.get("fwd_atts", True):
        files += [(p.get_filename() or "unnamed", p.get_payload(decode=True) or b"")
                  for p in attachment_parts(parse_raw(cfg, orig))]
    mime, rcpts = compose.build(acct, compose.addr_list([body.get("to", "")]), body.get("subject", ""),
                                body.get("body", ""), compose.addr_list([body.get("cc", "")]),
                                compose.addr_list([body.get("bcc", "")]), in_reply_to=irt, references=refs,
                                attach_data=files, signature=bool(body.get("signature", True)))
    res = approve.send_now(cfg, store, acct, mime, rcpts, reply_msg=orig["id"] if mode == "reply" and orig else None)
    if mode == "reply" and orig:
        try:
            imapops.set_flag(store, cfg.accounts, [orig["id"]], "answered")
        except Exception as e:
            log(f"ui: could not set \\Answered: {e}")
    log(f"ui: {res}")
    app.events.publish({"t": "drafts", "n": len(store.pending())})
    return {"ok": res}


def draft_action(app: App, ref: str, do: str) -> dict:
    store = app.store()
    did = parse_id(ref, draft=True)
    res = (approve.send_draft(app.cfg, store, did, via="ui") if do == "send"
           else approve.reject_draft(store, did, via="ui"))
    log(f"ui: {res}")
    app.events.publish({"t": "drafts", "n": len(store.pending())})
    return {"ok": res}


# ---- HTTP -----------------------------------------------------------------------------------

def make_handler(app: App, hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            pass

        def _send(self, code: int, body: bytes | str, ctype: str = "application/json",
                  headers: dict | None = None) -> None:
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            h = {"Content-Type": ctype + ("; charset=utf-8" if ctype.startswith("text") or "json" in ctype else ""),
                 "Content-Length": str(len(data)), "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                 "Referrer-Policy": "no-referrer", "Content-Security-Policy": APP_CSP, "X-Frame-Options": "DENY",
                 **(headers or {})}
            for k, v in h.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code: int = 200, headers: dict | None = None) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False, separators=(",", ":")), headers=headers)

        def _err(self, code: int, msg: str) -> None:
            self._json({"error": msg}, code)

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in hosts

        def do_GET(self) -> None:
            if not self._host_ok():
                return self._err(403, "bad host")
            url = urllib.parse.urlsplit(self.path)
            path, q = url.path, {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            if path == "/" or path.startswith("/static/"):
                name = "index.html" if path == "/" else path[8:]
                f = STATIC / name
                if "/" in name or name.startswith(".") or not f.is_file() or f.suffix not in STATIC_TYPES:
                    return self._err(404, "not found")
                return self._send(200, f.read_bytes(), STATIC_TYPES[f.suffix])
            sess = app.session(self.headers.get("Cookie"))
            if path == "/api/state":
                return self._json(state(app, sess))
            if not sess and not app.readonly:
                return self._err(401, "login required")
            try:
                if path == "/api/events":
                    return self._events()
                if path == "/api/list":
                    return self._json(list_msgs(app, q))
                if path == "/api/drafts":
                    return self._json(drafts(app))
                if path == "/api/folders":
                    return self._json(app.server_folders(q.get("acct", "")))
                if m := re.fullmatch(r"/api/compose/([0-9a-z]+)", path):
                    return self._json(compose_fields(app, parse_id(m.group(1)), q.get("mode", "reply")))
                m = re.fullmatch(r"/api/(msg|thread)/([0-9a-z]+)(?:/(html|att/(\d+)))?", path)
                if not m:
                    return self._err(404, "not found")
                rid = parse_id(m.group(2))
                if m.group(1) == "thread":
                    return self._json(thread(app, rid))
                if not m.group(3):
                    return self._json(message(app, rid))
                r = app.store().get(rid)
                if not r:
                    return self._err(404, "no such message")
                msg = parse_raw(app.cfg, r)
                if m.group(3) == "html":
                    images = q.get("images") == "1"
                    doc, _ = sanitize(html_body(msg) or "", cid_images(msg), images)
                    return self._send(200, doc, "text/html", {"Content-Security-Policy": mail_csp(images),
                                                              "X-Frame-Options": "SAMEORIGIN"})
                parts = attachment_parts(msg)
                i = int(m.group(4))
                if i >= len(parts):
                    return self._err(404, "no such attachment")
                name = parts[i].get_filename() or "attachment"
                disp = "attachment; filename*=UTF-8''" + urllib.parse.quote(name)
                return self._send(200, parts[i].get_payload(decode=True) or b"", "application/octet-stream",
                                  {"Content-Disposition": disp})
            except (LookupError, ValueError) as e:
                return self._err(404, str(e))
            except Exception as e:
                log(f"ui: GET {path}: {type(e).__name__}: {e}")
                return self._err(500, f"{type(e).__name__}: {e}")

        def _events(self) -> None:
            self.send_response(200)
            for k, v in {"Content-Type": "text/event-stream", "Cache-Control": "no-store",
                         "X-Content-Type-Options": "nosniff"}.items():
                self.send_header(k, v)
            self.end_headers()
            self.close_connection = True
            seq = app.events.seq
            try:
                self.wfile.write(b": hello\n\n")
                self.wfile.flush()
                while True:
                    seq, evs = app.events.wait(seq, 20)
                    for ev in evs:
                        self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
                    if not evs:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except OSError:
                return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_POST:
                self.close_connection = True
                return self._err(413, "too large")
            raw = self.rfile.read(length)  # always consume the body (keep-alive)
            origin = self.headers.get("Origin")
            if not self._host_ok() or (origin and origin.split("//", 1)[-1] not in hosts):
                return self._err(403, "bad host or origin")
            try:
                body = json.loads(raw or b"{}")
                assert isinstance(body, dict)
            except (ValueError, AssertionError):
                return self._err(400, "bad json")
            path = self.path
            if path == "/api/login":
                res = app.login(str(body.get("passphrase", "")))
                if not res:
                    return self._err(403, "wrong passphrase" if not app.readonly else "no passphrase set")
                cookie = f"{COOKIE}={res[0]}; Path=/; HttpOnly; SameSite=Strict"
                return self._json({"ok": "logged in", "csrf": res[1]}, headers={"Set-Cookie": cookie})
            sess = app.session(self.headers.get("Cookie"))
            if app.readonly:
                return self._err(403, "read-only: no UI passphrase set (run mg ui --set-passphrase)")
            if not sess:
                return self._err(401, "login required")
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), sess["csrf"]):
                return self._err(403, "bad csrf token")
            try:
                if path == "/api/logout":
                    app.logout(sess["token"])
                    return self._json({"ok": "logged out"}, headers={"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0"})
                if path == "/api/act":
                    return self._json(act(app, body))
                if path == "/api/send":
                    return self._json(send(app, body))
                if path == "/api/sync":
                    app.wake.set()
                    return self._json({"ok": "sync started"})
                if m := re.fullmatch(r"/api/drafts/(d[0-9a-z]+)", path):
                    return self._json(draft_action(app, m.group(1), body.get("do", "")))
                return self._err(404, "not found")
            except (ValueError, LookupError, approve.ApprovalError) as e:
                return self._err(400, str(e))
            except Exception as e:
                log(f"ui: POST {path}: {type(e).__name__}: {e}")
                return self._err(500, f"{type(e).__name__}: {e}")

    return Handler


def server(cfg: Config, db: Path, host: str = "127.0.0.1", port: int = 8766,
           passfile: Path | None = None) -> tuple[ThreadingHTTPServer, App]:
    """Create (not start) the UI server; only loopback addresses are allowed."""
    host = host.strip("[]")
    if host not in LOOPBACK:
        raise ValueError("mg ui may only listen on 127.0.0.1, ::1 or localhost (use an SSH tunnel for remote use)")
    app = App(cfg, db, passfile)
    cls = type("V6Server", (ThreadingHTTPServer,), {"address_family": socket.AF_INET6}) if ":" in host \
        else ThreadingHTTPServer
    srv = cls((host, port), lambda *a: None)
    srv.daemon_threads = True
    p = srv.server_address[1]
    srv.RequestHandlerClass = make_handler(app, {f"{h}:{p}" for h in ("127.0.0.1", "localhost", "[::1]")})
    return srv, app


def run(cfg: Config, db: Path, host: str, port: int, open_browser: bool = False) -> None:
    """Serve until Ctrl-C, with background sync, IMAP IDLE and draft watching."""
    import signal
    srv, app = server(cfg, db, host, port)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: (stop.set(), app.wake.set()))
    url = f"http://{'localhost' if host in ('127.0.0.1', 'localhost') else '[::1]'}:{srv.server_address[1]}/"
    log(f"mailgate ui on {url} ({'READ-ONLY, no passphrase set' if app.readonly else 'login required'})")
    threads = [threading.Thread(target=srv.serve_forever, daemon=True),
               threading.Thread(target=app.sync_loop, args=(stop,), daemon=True),
               threading.Thread(target=app.drafts_loop, args=(stop,), daemon=True)]
    if cfg.ui.idle:
        threads += [threading.Thread(target=app.idle_loop, args=(a, stop), daemon=True)
                    for a in cfg.accounts.values() if "INBOX" in a.folders]
    for t in threads:
        t.start()
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        while not stop.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        app.wake.set()
        srv.shutdown()

