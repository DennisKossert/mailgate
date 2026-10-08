"""Minimal fake IMAP, SMTP and ntfy servers for tests (localhost only)."""
from __future__ import annotations

import base64
import json
import re
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = re.compile(rb'"((?:[^"\\]|\\.)*)"|\(([^)]*)\)|(\S+)')


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeIMAP:
    """IMAP4rev1 subset: CAPABILITY LOGIN SELECT EXAMINE LIST UID SEARCH/FETCH/STORE/COPY/MOVE/EXPUNGE,
    EXPUNGE, APPEND, IDLE, LOGOUT. Drop "MOVE"/"UIDPLUS"/"IDLE" from .caps to test fallbacks."""

    def __init__(self, password: str = "secret"):
        self.password = password
        self.folders: dict[str, dict] = {"INBOX": {"uv": 1, "next": 1, "msgs": []},
                                         "Sent": {"uv": 1, "next": 1, "msgs": []}}
        self.special = {"Sent": "\\Sent", "Trash": "\\Trash", "Archive": "\\Archive"}
        self.caps = ["MOVE", "UIDPLUS", "IDLE", "SPECIAL-USE"]
        self.commands: list[str] = []
        self.idlers: list = []
        self.lock = threading.Lock()
        fake = self

        class H(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                fake._session(self)
        self.srv = _Server(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, args=(0.05,), daemon=True).start()

    def stop(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def add(self, folder: str, raw: bytes, flags: str = "") -> int:
        with self.lock:
            f = self.folders.setdefault(folder, {"uv": 1, "next": 1, "msgs": []})
            uid = f["next"]
            f["next"] += 1
            f["msgs"].append([uid, flags, raw])
            n = len(f["msgs"])
        for name, w in list(self.idlers):
            if name == folder:
                try:
                    w(f"* {n} EXISTS\r\n".encode())
                except OSError:
                    pass
        return uid

    def flags(self, folder: str) -> dict[int, str]:
        return {m[0]: m[1] for m in self.folders[folder]["msgs"]}

    def _store(self, m: list, op: str, flags: str) -> None:
        have = set(m[1].split())
        want = set(flags.split())
        have = want if op.startswith("FLAGS") else (have | want if op.startswith("+") else have - want)
        m[1] = " ".join(sorted(have))

    def _uids(self, f: dict, spec: str) -> list[list]:
        top = max([m[0] for m in f["msgs"]] or [0])
        want: set[int] = set()
        for part in spec.split(","):
            if ":" in part:
                a, b = (top if x == "*" else int(x) for x in part.split(":"))
                want |= set(range(min(a, b), max(a, b) + 1))
            else:
                want.add(top if part == "*" else int(part))
        return [m for m in f["msgs"] if m[0] in want]

    def _session(self, h) -> None:
        w = h.wfile.write
        w(b"* OK fake imap ready\r\n")
        cur, curname = None, ""
        while True:
            line = h.rfile.readline()
            if not line:
                return
            toks = [a or b or c for a, b, c in TOKEN.findall(line.strip())]
            if len(toks) < 2:
                continue
            tag, cmd, args = toks[0], toks[1].upper().decode(), [t.decode() for t in toks[2:]]
            self.commands.append(" ".join([cmd] + args[:2]))
            t = tag
            if cmd == "CAPABILITY":
                w(("* CAPABILITY IMAP4rev1 AUTH=PLAIN " + " ".join(self.caps)).encode() + b"\r\n" + t + b" OK done\r\n")
            elif cmd == "LOGIN":
                ok = args[1] == self.password
                w(t + (b" OK logged in\r\n" if ok else b" NO bad credentials\r\n"))
            elif cmd in ("SELECT", "EXAMINE"):
                cur = self.folders.get(args[0])
                if cur is None:
                    w(t + b" NO no such folder\r\n")
                    continue
                w(f"* {len(cur['msgs'])} EXISTS\r\n* OK [UIDVALIDITY {cur['uv']}] ok\r\n"
                  f"* OK [UIDNEXT {cur['next']}] ok\r\n".encode() + t +
                  (b" OK [READ-ONLY] done\r\n" if cmd == "EXAMINE" else b" OK [READ-WRITE] done\r\n"))
                curname = args[0]
            elif cmd == "LIST":
                for name in self.folders:
                    attrs = " ".join(["\\HasNoChildren"] + ([self.special[name]] if name in self.special else []))
                    w(f'* LIST ({attrs}) "/" "{name}"\r\n'.encode())
                w(t + b" OK done\r\n")
            elif cmd == "UID" and args[0].upper() == "STORE":
                with self.lock:
                    for m in self._uids(cur, args[1]):
                        self._store(m, args[2].upper(), args[3])
                w(t + b" OK stored\r\n")
            elif cmd == "UID" and args[0].upper() in ("COPY", "MOVE") and args[0].upper() in self.caps + ["COPY"]:
                if args[2] not in self.folders:
                    w(t + b" NO [TRYCREATE] no such folder\r\n")
                    continue
                msgs = self._uids(cur, args[1])
                for m in msgs:
                    self.add(args[2], m[2], " ".join(f for f in m[1].split() if f != "\\Deleted"))
                if args[0].upper() == "MOVE":
                    with self.lock:
                        cur["msgs"] = [m for m in cur["msgs"] if m not in msgs]
                w(t + b" OK done\r\n")
            elif cmd == "UID" and args[0].upper() == "EXPUNGE" and "UIDPLUS" in self.caps:
                want = {m[0] for m in self._uids(cur, args[1])}
                with self.lock:
                    cur["msgs"] = [m for m in cur["msgs"] if not (m[0] in want and "\\Deleted" in m[1])]
                w(t + b" OK expunged\r\n")
            elif cmd == "EXPUNGE":
                with self.lock:
                    cur["msgs"] = [m for m in cur["msgs"] if "\\Deleted" not in m[1]]
                w(t + b" OK expunged\r\n")
            elif cmd == "IDLE" and "IDLE" in self.caps:
                entry = (curname, w)
                self.idlers.append(entry)
                w(b"+ idling\r\n")
                h.rfile.readline()  # DONE
                self.idlers.remove(entry)
                w(t + b" OK idle done\r\n")
            elif cmd == "UID" and args[0].upper() == "SEARCH":
                rest = args[1:]
                msgs = self._uids(cur, rest[rest.index("UID") + 1]) if "UID" in rest else cur["msgs"]
                if "DELETED" in [r.upper() for r in rest]:
                    msgs = [m for m in msgs if "\\Deleted" in m[1]]
                w(("* SEARCH " + " ".join(str(m[0]) for m in msgs)).rstrip().encode() + b"\r\n" + t + b" OK done\r\n")
            elif cmd == "UID" and args[0].upper() == "FETCH":
                items = args[2].upper()
                for m in list(self._uids(cur, args[1])):
                    seq = cur["msgs"].index(m) + 1
                    head = f"* {seq} FETCH (UID {m[0]} FLAGS ({m[1]})"
                    if "BODY" in items:
                        w(f"{head} RFC822.SIZE {len(m[2])} BODY[] {{{len(m[2])}}}\r\n".encode() + m[2] + b")\r\n")
                    else:
                        w(head.encode() + b")\r\n")
                w(t + b" OK done\r\n")
            elif cmd == "APPEND":
                n = int(re.search(rb"\{(\d+)\}$", line.strip()).group(1))
                w(b"+ ready\r\n")
                data = h.rfile.read(n)
                h.rfile.readline()
                flags = next((a for a in args[1:] if a.startswith("\\")), "")
                self.add(args[0], data, flags)
                w(t + b" OK appended\r\n")
            elif cmd == "LOGOUT":
                w(b"* BYE\r\n" + t + b" OK bye\r\n")
                return
            else:
                w(t + b" OK\r\n")


class FakeSMTP:
    """SMTP subset with AUTH PLAIN; stores (mail_from, rcpts, data) in .sent."""

    def __init__(self, password: str = "secret"):
        self.password = password
        self.sent: list[tuple[str, list[str], bytes]] = []
        fake = self

        class H(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                fake._session(self)
        self.srv = _Server(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, args=(0.05,), daemon=True).start()

    def stop(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def _session(self, h) -> None:
        w = h.wfile.write
        w(b"220 fake smtp\r\n")
        frm, rcpts = "", []
        while line := h.rfile.readline():
            cmd = line.strip().decode()
            up = cmd.upper()
            if up.startswith(("EHLO", "HELO")):
                w(b"250-fake\r\n250-AUTH PLAIN\r\n250 8BITMIME\r\n")
            elif up.startswith("AUTH PLAIN"):
                pw = base64.b64decode(cmd.split()[2]).split(b"\0")[2].decode()
                w(b"235 ok\r\n" if pw == self.password else b"535 bad\r\n")
            elif up.startswith("MAIL FROM:"):
                frm, rcpts = cmd[10:].split()[0].strip("<>"), []
                w(b"250 ok\r\n")
            elif up.startswith("RCPT TO:"):
                rcpts.append(cmd[8:].strip().strip("<>"))
                w(b"250 ok\r\n")
            elif up == "DATA":
                w(b"354 go\r\n")
                buf = []
                while (ln := h.rfile.readline()) != b".\r\n":
                    buf.append(ln[1:] if ln.startswith(b"..") else ln)
                self.sent.append((frm, rcpts, b"".join(buf)))
                w(b"250 queued\r\n")
            elif up == "QUIT":
                w(b"221 bye\r\n")
                return
            else:
                w(b"250 ok\r\n")


class FakeNtfy:
    """ntfy subset: JSON publish to /, publish to /topic, /topic/json stream (then closes), /v1/health."""

    def __init__(self):
        self.published: list[dict] = []
        self.topics: dict[str, list[dict]] = {}
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/":
                    fake.published.append(json.loads(body))
                else:
                    fake.post(self.path.strip("/"), body.decode())
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def do_GET(self) -> None:
                self.send_response(200)
                self.end_headers()
                if self.path.startswith("/v1/health"):
                    self.wfile.write(b'{"healthy":true}')
                    return
                topic = self.path.split("/")[1]
                self.wfile.write(b'{"event":"open"}\n')
                for ev in fake.topics.get(topic, []):
                    self.wfile.write(json.dumps(ev).encode() + b"\n")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, args=(0.05,), daemon=True).start()

    def post(self, topic: str, message: str) -> None:
        lst = self.topics.setdefault(topic, [])
        lst.append({"id": f"m{len(lst)}", "event": "message", "topic": topic, "message": message})

    def stop(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
