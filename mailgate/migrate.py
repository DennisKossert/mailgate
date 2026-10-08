"""Move mailgate to another device: `mg export` / `mg import`, as a file or by LAN pairing.

A bundle holds the config (accounts, rules, signatures, UI settings), watcher state, pending
drafts, reminders and the ntfy position. It never holds the mail cache (the new device
resyncs from IMAP) or the UI passphrase hash. Secrets (account passwords, ntfy token) are
only included on request and only encrypted: AES-256-GCM with a key derived by scrypt from
a one-time code. Encryption needs the optional `cryptography` package (mailgate[crypto]).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, config_path, parse
from .store import Store

FORMAT = "mailgate-export"
AAD = b"mailgate-export-v1"
CODE_CHARS = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
PAIR_PORT = 8767
PAIR_TTL = 600
PAIR_MAX_FAILS = 5


class MigrateError(ValueError):
    """Export/import failed."""


def have_crypto() -> bool:
    try:
        import cryptography.hazmat.primitives.ciphers.aead  # noqa: F401
        return True
    except ImportError:
        return False


# ---- one-time code and encryption ----------------------------------------------------------

def new_code() -> str:
    """60-bit one-time code like 7KQ4-29XF-M3PA."""
    raw = "".join(secrets.choice(CODE_CHARS) for _ in range(12))
    return "-".join(raw[i:i + 4] for i in range(0, 12, 4))


def norm_code(code: str) -> str:
    c = code.upper().replace("-", "").replace(" ", "").replace("O", "0").replace("I", "1").replace("L", "1")
    if len(c) != 12 or any(ch not in CODE_CHARS for ch in c):
        raise MigrateError("the code has 12 characters like 7KQ4-29XF-M3PA")
    return c


def _keys(code: str, salt: bytes, n: int = 2 ** 15) -> tuple[bytes, bytes]:
    """(encryption key, pairing auth key) from the code."""
    k = hashlib.scrypt(norm_code(code).encode(), salt=salt, n=n, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=64)
    return k[:32], k[32:]


def encrypt(data: dict, code: str) -> dict:
    if not have_crypto():
        raise MigrateError("encryption needs the cryptography package: pipx inject mailgate cryptography "
                           "(or pip install 'mailgate[crypto]')")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    key, _ = _keys(code, salt)
    ct = AESGCM(key).encrypt(nonce, json.dumps(data).encode(), AAD)
    return {"format": FORMAT, "enc": {"alg": "AES-256-GCM", "kdf": "scrypt", "n": 2 ** 15, "r": 8, "p": 1,
                                      "salt": salt.hex(), "nonce": nonce.hex()},
            "ct": base64.b64encode(ct).decode()}


def decrypt(env: dict, code: str) -> dict:
    if not have_crypto():
        raise MigrateError("this bundle is encrypted; install the cryptography package to import it")
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    e = env["enc"]
    key, _ = _keys(code, bytes.fromhex(e["salt"]), int(e["n"]))
    try:
        return json.loads(AESGCM(key).decrypt(bytes.fromhex(e["nonce"]), base64.b64decode(env["ct"]), AAD))
    except InvalidTag:
        raise MigrateError("wrong code or damaged bundle") from None


# ---- bundle ---------------------------------------------------------------------------------

def bundle(cfg: Config, store: Store, with_secrets: bool = False) -> dict:
    data = {"format": FORMAT, "version": 1, "created": int(time.time()), "mailgate": __version__,
            "config": config_path().read_text(), "kv": {k: store.kv_get(k) for k in ("ntfy_since",)
                                                          if store.kv_get(k)},
            "watchers": [], "drafts": [], "plugin_data": []}
    for w in store.q("SELECT name, created FROM watchers"):
        seen = [r[0] for r in store.q("SELECT m.msgid FROM seen s JOIN msgs m ON m.id=s.msg WHERE s.watcher=? "
                                      "AND m.msgid!='' UNION SELECT msgid FROM seen_ids WHERE watcher=?",
                                      (w["name"], w["name"]))]
        data["watchers"].append({"name": w["name"], "created": w["created"], "seen": seen})
    for d in store.q("SELECT * FROM drafts WHERE status IN ('pending','scheduled')"):
        data["drafts"].append({k: d[k] for k in ("acct", "created", "expires", "status", "sha256", "token_hash",
                                                 "sender", "rcpts", "to_addr", "subject", "send_at")}
                              | {"mime": base64.b64encode(bytes(d["mime"])).decode()})
    for r in store.q("SELECT * FROM plugin_data"):  # reminders, unsubscribe log, ... (portable keys)
        data["plugin_data"].append({k: r[k] for k in r.keys()})
    if with_secrets:
        sec: dict = {"accounts": {}}
        for a in cfg.accounts.values():
            try:
                sec["accounts"][a.name] = a.password()
            except ConfigError as e:
                raise MigrateError(f"cannot read the password of {a.name}: {e}") from None
        if cfg.ntfy and (cfg.ntfy.token_cmd or cfg.ntfy.token_env):
            sec["ntfy_token"] = cfg.ntfy.auth_header()["Authorization"].removeprefix("Bearer ")
        data["secrets"] = sec
    return data


def export_file(cfg: Config, store: Store, out: Path, with_secrets: bool) -> str | None:
    """Write the bundle; returns the one-time code if it was encrypted."""
    data = bundle(cfg, store, with_secrets)
    code = None
    if with_secrets:
        code = new_code()
        data = encrypt(data, code)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):  # also when the file existed
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    return code


def store_secret(acct: str, password: str) -> str | None:
    """Put a password into the OS keyring; returns the password_cmd to read it, or None if no keyring tool."""
    if sys.platform == "darwin" and shutil.which("security"):
        subprocess.run(["security", "add-generic-password", "-U", "-s", "mailgate", "-a", acct, "-w", password],
                       check=True, capture_output=True)
        return f"security find-generic-password -s mailgate -a {acct} -w"
    if shutil.which("secret-tool"):
        subprocess.run(["secret-tool", "store", "--label", f"mailgate {acct}", "service", "mailgate", "account", acct],
                       input=password, text=True, check=True, capture_output=True)
        return f"secret-tool lookup service mailgate account {acct}"
    return None


def _set_password_cmd(text: str, acct: str, cmd: str) -> str:
    """Point [accounts.<acct>] at a new password_cmd (replacing password_cmd/password_env)."""
    head = re.search(rf"^\[accounts\.{re.escape(acct)}\]\s*$", text, re.M)
    if not head:
        return text
    end = re.search(r"^\[", text[head.end():], re.M)
    stop = head.end() + (end.start() if end else len(text) - head.end())
    body = re.sub(r"^\s*password_(cmd|env)\s*=.*\n?", "", text[head.end():stop], flags=re.M)
    return text[:head.end()] + f"\npassword_cmd = {json.dumps(cmd)}" + body + text[stop:]


def import_bundle(data: dict, store: Store, force: bool = False, log=print) -> None:
    if data.get("format") != FORMAT or "config" not in data:
        raise MigrateError("not a mailgate export")
    text = data["config"]
    for acct, pw in (data.get("secrets") or {}).get("accounts", {}).items():
        cmd = store_secret(acct, pw)
        if cmd:
            text = _set_password_cmd(text, acct, cmd)
            log(f"{acct}: password stored in the system keyring")
        else:
            log(f"{acct}: no keyring tool found (secret-tool / security); set password_cmd or password_env yourself")
    if (data.get("secrets") or {}).get("ntfy_token"):
        log("ntfy token: not stored automatically; set token_cmd/token_env in [approval.ntfy]")
    try:
        parse(__import__("tomllib").loads(text))
    except Exception as e:
        raise MigrateError(f"config in the bundle is not valid here: {e}") from None
    p = config_path()
    if p.exists():
        if not force:
            raise MigrateError(f"{p} exists; use --force (a backup is kept)")
        p.rename(p.with_name(f"config.toml.bak-{int(time.time())}"))
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):  # also when the file existed
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    db = store.db
    for k, v in (data.get("kv") or {}).items():
        store.kv_set(k, v)
    for w in data.get("watchers", []):
        db.execute("INSERT OR REPLACE INTO watchers(name, created, since) VALUES(?,?,?)",
                   (w["name"], w["created"], data["created"]))
        db.executemany("INSERT OR IGNORE INTO seen_ids VALUES(?,?)", [(w["name"], m) for m in w["seen"]])
    for d in data.get("drafts", []):
        if not store.one("SELECT 1 FROM drafts WHERE sha256=?", (d["sha256"],)):
            store.add_draft(**{k: v for k, v in d.items() if k != "mime"}, mime=base64.b64decode(d["mime"]))
    for r in data.get("plugin_data", []):
        db.execute("INSERT OR REPLACE INTO plugin_data VALUES(?,?,?,?,?)",
                   (r["plugin"], r["kind"], r["key"], r["data"], r["ts"]))
    log(f"imported config, {len(data.get('watchers', []))} watchers, {len(data.get('drafts', []))} drafts, "
        f"{len(data.get('plugin_data', []))} plugin records. Next: mg doctor && mg sync")


def read_file(path: Path, code: str | None) -> dict:
    env = json.loads(Path(path).read_text())
    if "enc" in env:
        if not code:
            raise MigrateError("this bundle is encrypted: pass --code")
        return decrypt(env, code)
    return env


# ---- LAN pairing ----------------------------------------------------------------------------

def lan_ip() -> str:
    """The address other devices in the LAN can reach (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Pairing:
    """Short-lived HTTP server that hands out one encrypted bundle to whoever knows the code.

    The code never travels over the network: the client proves it with an HMAC from the
    scrypt-derived auth key. Single use, PAIR_TTL seconds, locked after PAIR_MAX_FAILS wrong tries.
    """

    def __init__(self, data: dict, host: str | None = None, port: int = PAIR_PORT, code: str | None = None):
        self.code = code or new_code()
        self.env = encrypt(data, self.code)
        _, self.auth = _keys(self.code, bytes.fromhex(self.env["enc"]["salt"]))
        self.fails = 0
        self.state = "waiting"  # waiting | done | locked | expired
        self.done = threading.Event()
        self.lock = threading.Lock()
        pairing = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a) -> None:
                pass

            def _send(self, code: int, obj: dict) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == "/mgpair/salt":
                    return self._send(200, {"enc": {k: v for k, v in pairing.env["enc"].items() if k != "nonce"}})
                if self.path != "/mgpair/bundle":
                    return self._send(404, {"error": "not found"})
                with pairing.lock:  # one guess at a time, one second per wrong guess
                    if pairing.state != "waiting":
                        return self._send(410, {"error": pairing.state})
                    want = hmac.new(pairing.auth, b"mgpair-fetch", "sha256").hexdigest()
                    if not hmac.compare_digest(self.headers.get("X-MG-Auth", ""), want):
                        pairing.fails += 1
                        time.sleep(1)
                        if pairing.fails >= PAIR_MAX_FAILS:
                            pairing.finish("locked")
                        return self._send(403, {"error": "wrong code"})
                    self._send(200, pairing.env)
                    pairing.finish("done")

        self.srv = ThreadingHTTPServer((host or lan_ip(), port), H)
        self.host, self.port = self.srv.server_address[:2]
        self.url = f"mgpair://{self.host}:{self.port}/{self.code}"

    def finish(self, state: str) -> None:
        self.state = state
        self.done.set()
        threading.Thread(target=self.srv.shutdown, daemon=True).start()

    def serve(self, ttl: float = PAIR_TTL) -> str:
        """Serve until fetched, locked or expired; returns the final state."""
        threading.Thread(target=self.srv.serve_forever, args=(0.2,), daemon=True).start()
        if not self.done.wait(ttl):
            with self.lock:
                if self.state == "waiting":
                    self.finish("expired")
        self.done.wait(2)
        self.srv.server_close()
        return self.state

    def start(self, ttl: float = PAIR_TTL) -> None:
        threading.Thread(target=self.serve, args=(ttl,), daemon=True).start()


def parse_target(s: str) -> tuple[str, int, str]:
    """'mgpair://host:port/CODE' or 'CODE@host[:port]' -> (host, port, code)."""
    if m := re.fullmatch(r"mgpair://([^/:]+)(?::(\d+))?/([0-9A-Za-z-]+)", s.strip()):
        return m.group(1), int(m.group(2) or PAIR_PORT), m.group(3)
    if m := re.fullmatch(r"([0-9A-Za-z-]+)@([^:]+)(?::(\d+))?", s.strip()):
        return m.group(2), int(m.group(3) or PAIR_PORT), m.group(1)
    raise MigrateError("use mg import CODE@HOST[:PORT] or mg import mgpair://HOST:PORT/CODE")


def fetch_pair(target: str) -> dict:
    host, port, code = parse_target(target)
    base = f"http://{host}:{port}/mgpair"
    with urllib.request.urlopen(base + "/salt", timeout=15) as r:
        enc = json.load(r)["enc"]
    _, auth = _keys(code, bytes.fromhex(enc["salt"]), int(enc["n"]))
    req = urllib.request.Request(base + "/bundle",
                                 headers={"X-MG-Auth": hmac.new(auth, b"mgpair-fetch", "sha256").hexdigest()})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            env = json.load(r)
    except urllib.error.HTTPError as e:
        raise MigrateError(f"pairing refused: HTTP {e.code} ({'wrong code' if e.code == 403 else 'expired or used'})")
    return decrypt(env, code)


# ---- QR -----------------------------------------------------------------------------------------

def qr_matrix(text: str) -> list[list[bool]]:
    from .qrcodegen import QrCode
    q = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    return [[q.get_module(x, y) for x in range(q.get_size())] for y in range(q.get_size())]


def qr_terminal(text: str) -> str:
    """QR code with unicode half blocks, dark on light (ANSI colours), 2-module quiet zone."""
    m = qr_matrix(text)
    n, pad = len(m), 2
    get = lambda x, y: 0 <= x < n and 0 <= y < n and m[y][x]  # noqa: E731
    lines = []
    for y in range(-pad, n + pad, 2):
        row = "".join({(0, 0): " ", (1, 0): "▀", (0, 1): "▄", (1, 1): "█"}[(int(get(x, y)), int(get(x, y + 1)))]
                      for x in range(-pad, n + pad))
        lines.append("\x1b[30;47m" + row + "\x1b[0m")
    return "\n".join(lines)
