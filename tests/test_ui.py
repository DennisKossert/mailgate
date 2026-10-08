import base64
import http.client
import json
import os
import re
import threading
import time
import unittest
from email import message_from_bytes
from email.message import EmailMessage

from mailgate import approve, compose, webui

from .helpers import Env, make_mail

PASS = "correct horse battery"


def html_mail() -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "Shop <shop@example.com>", "jane@example.com", "Angebot"
    m["Message-ID"] = "<h1@example.com>"
    m.set_content("Angebot im Text")
    m.add_alternative('<html><head><script>alert(1)</script><meta http-equiv="refresh" content="0;url=https://x">'
                      '</head><body onload="evil()"><p onclick="x()">Hallo <b>Welt</b></p>'
                      '<img src="https://tracker.example.com/p.gif"><img src="cid:logo@example.com">'
                      '<a href="javascript:alert(1)">bad</a><a href="https://example.com/x">gut</a>'
                      '<form action="https://evil.example.com"><input name=q></form>'
                      '<iframe src="https://evil.example.com"></iframe><style>p{color:red}</style></body></html>',
                      subtype="html")
    m.get_payload()[1].add_related(b"\x89PNG fake", maintype="image", subtype="png", cid="<logo@example.com>")
    return m.as_bytes()


class SanitizeTest(unittest.TestCase):
    def test_policy(self):
        doc, blocked = webui.sanitize('<p onclick="x()">a<script>alert(1)</script></p><img src="http://t.example.com/a">'
                                      '<img srcset="https://x 1x" src="cid:c1"><a href="javascript:x">j</a>'
                                      '<svg><script>bad()</script></svg><base href="https://evil.example.com/">'
                                      '<div style="background:url(https://t.example.com/bg)">s</div>'
                                      '<style>a{}</style ><style></style><style>x</style>',
                                      {"c1": "data:image/png;base64,AAAA"})
        low = doc.lower()
        for bad in ("<script", "onclick", "javascript:", "srcset", "<svg", "evil.example.com", "http://t.example.com"):
            self.assertNotIn(bad, low)
        self.assertIn('src="data:image/png;base64,AAAA"', doc)
        self.assertEqual(blocked, 2)
        self.assertIn('<a target="_blank" rel="noopener noreferrer">j</a>', doc)
        doc, blocked = webui.sanitize('<img src="https://t.example.com/a">', images=True)
        self.assertIn('src="https://t.example.com/a"', doc)
        self.assertEqual(blocked, 0)

    def test_style_cannot_break_out(self):
        doc, _ = webui.sanitize("<style>p{}</style><script>alert(1)</script><style>a</style>")
        self.assertNotIn("<script", doc)
        doc, _ = webui.sanitize("<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>")
        self.assertIn("&lt;script&gt;", doc)

    def test_csp(self):
        self.assertIn("img-src data: cid:;", webui.mail_csp(False))
        self.assertIn("https:", webui.mail_csp(True))
        self.assertNotIn("script-src", webui.mail_csp(True))
        self.assertIn("default-src 'none'", webui.mail_csp(True))


class PassphraseTest(Env):
    def test_hash_only_and_mode(self):
        p = self.dir / "ui-passphrase"
        webui.set_passphrase(PASS, p)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        self.assertNotIn(PASS, p.read_text())
        self.assertEqual(json.loads(p.read_text())["kdf"], "scrypt")
        self.assertTrue(webui.check_passphrase(PASS, p))
        self.assertFalse(webui.check_passphrase("wrong", p))
        with self.assertRaises(ValueError):
            webui.set_passphrase("short", p)


class UiTest(Env):
    passphrase = True

    def setUp(self) -> None:
        super().setUp()
        webui.log = lambda msg: None
        self.imap.folders.update({n: {"uv": 1, "next": 1, "msgs": []} for n in ("Trash", "Archive")})
        self.imap.add("INBOX", make_mail("Termin", "Passt Donnerstag?", msgid="<t1@example.org>",
                                         attach=b"%PDF" * 10))
        self.imap.add("INBOX", html_mail())
        self.mg("sync")
        if self.passphrase:
            webui.set_passphrase(PASS, webui.pass_path())
        self.srv, self.app = webui.server(self.cfg, self.dir / "mail.db", "127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, args=(0.05,), daemon=True).start()
        self.port = self.srv.server_address[1]
        self.cookie = self.csrf = ""

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def req(self, method: str, path: str, body=None, headers: dict | None = None, csrf: bool = True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if self.cookie:
            h["Cookie"] = self.cookie
        if method == "POST":
            h["Content-Type"] = "application/json"
            if csrf and self.csrf:
                h["X-CSRF-Token"] = self.csrf
        h.update(headers or {})
        c.request(method, path, json.dumps(body or {}) if method == "POST" else None, h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, r.headers, data

    def js(self, method: str, path: str, body=None, **kw):
        st, _, data = self.req(method, path, body, **kw)
        return st, json.loads(data or b"{}")

    def login(self) -> None:
        st, hdrs, data = self.req("POST", "/api/login", {"passphrase": PASS})
        self.assertEqual(st, 200)
        sc = hdrs["Set-Cookie"]
        self.assertIn("HttpOnly", sc)
        self.assertIn("SameSite=Strict", sc)
        self.cookie = sc.split(";")[0]
        self.assertGreaterEqual(len(self.cookie.split("=", 1)[1]), 43)  # 32 random bytes
        self.csrf = json.loads(data)["csrf"]

    def test_login_required(self):
        st, _, data = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"/static/app.js", data)
        st, d = self.js("GET", "/api/state")
        self.assertTrue(d["need_login"])
        self.assertEqual(d["accounts"], [])
        self.assertEqual(self.js("GET", "/api/list")[0], 401)
        self.assertEqual(self.js("GET", "/api/msg/1")[0], 401)
        self.assertEqual(self.js("POST", "/api/act", {"op": "read", "ids": ["1"]})[0], 401)
        t0 = time.time()
        self.assertEqual(self.js("POST", "/api/login", {"passphrase": "nope"})[0], 403)
        self.assertGreaterEqual(time.time() - t0, 0.9)  # failed logins are slow
        self.login()
        st, d = self.js("GET", "/api/state")
        self.assertFalse(d["need_login"])
        self.assertEqual(d["accounts"][0]["folders"][0], {"name": "INBOX", "n": 2, "u": 2})

    def test_csrf_host_origin_and_expiry(self):
        self.login()
        self.assertEqual(self.js("POST", "/api/act", {"op": "read", "ids": ["1"]}, csrf=False)[0], 403)
        self.assertEqual(self.js("POST", "/api/act", {"op": "read", "ids": ["1"]},
                                 headers={"X-CSRF-Token": "forged"})[0], 403)
        self.assertEqual(self.js("POST", "/api/act", {"op": "read", "ids": ["1"]},
                                 headers={"Origin": "http://evil.example.com"})[0], 403)
        self.assertEqual(self.js("GET", "/api/list", headers={"Host": "evil.example.com"})[0], 403)
        self.assertEqual(self.imap.flags("INBOX")[1], "")
        st, d = self.js("POST", "/api/act", {"op": "read", "ids": ["1"]})
        self.assertEqual(st, 200, d)
        self.assertEqual(self.imap.flags("INBOX")[1], "\\Seen")
        for s in self.app.sessions.values():
            s["last"] -= webui.SESSION_IDLE + 1
        self.assertEqual(self.js("GET", "/api/list")[0], 401)

    def test_list_msg_html_att(self):
        self.login()
        st, d = self.js("GET", "/api/list?folder=INBOX&n=1")
        self.assertEqual(st, 200)
        self.assertEqual(d["items"][0]["s"], "Angebot")
        st, d2 = self.js("GET", "/api/list?folder=INBOX&n=1&before=" + d["next"])
        self.assertEqual([i["s"] for i in d2["items"]], ["Termin"])
        st, d = self.js("GET", "/api/list?q=donnerstag")
        self.assertEqual(len(d["items"]), 1)
        st, m = self.js("GET", "/api/msg/2")
        self.assertTrue(m["html"])
        self.assertEqual(m["remote"], 1)
        st, hdrs, doc = self.req("GET", "/api/msg/2/html")
        self.assertEqual(st, 200)
        csp = hdrs["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("img-src data: cid:;", csp)
        self.assertIn("sandbox", csp)
        low = doc.decode().lower()
        for bad in ("<script", "onload", "onclick", "javascript:", "tracker.example.com", "<iframe", "<form",
                    "http-equiv", "evil.example.com"):
            self.assertNotIn(bad, low)
        self.assertIn("data:image/png;base64,", low)  # cid image inlined from the cache
        self.assertIn("<b>welt</b>", low)
        _, hdrs, doc = self.req("GET", "/api/msg/2/html?images=1")
        self.assertIn("tracker.example.com", doc.decode())
        self.assertIn("https:", hdrs["Content-Security-Policy"])
        st, hdrs, data = self.req("GET", "/api/msg/1/att/0")
        self.assertEqual((st, data), (200, b"%PDF" * 10))
        self.assertEqual(hdrs["Content-Type"], "application/octet-stream")
        self.assertIn("rechnung.pdf", hdrs["Content-Disposition"])
        self.assertEqual(self.req("GET", "/api/msg/1/att/5")[0], 404)
        st, th = self.js("GET", "/api/thread/1")
        self.assertEqual(th[0]["text"], "Passt Donnerstag?")
        self.assertEqual(self.req("GET", "/static/../webui.py")[0], 404)
        _, hdrs, _ = self.req("GET", "/")
        self.assertIn("frame-ancestors 'none'", hdrs["Content-Security-Policy"])

    def test_archive_trash_move(self):
        self.login()
        st, d = self.js("POST", "/api/act", {"op": "archive", "ids": ["1"]})
        self.assertEqual(d["folder"], "Archive")
        st, d = self.js("POST", "/api/act", {"op": "trash", "ids": ["2"]})
        self.assertEqual(d["folder"], "Trash")
        self.assertEqual(self.imap.folders["INBOX"]["msgs"], [])
        self.assertEqual(len(self.imap.folders["Trash"]["msgs"]), 1)
        self.assertEqual(self.js("GET", "/api/list?folder=INBOX")[1]["items"], [])
        st, d = self.js("GET", "/api/folders?acct=work")
        self.assertEqual(d[0], "INBOX")
        self.assertIn("Archive", d)

    def test_send_reply_without_queue(self):
        self.login()
        st, f = self.js("GET", "/api/compose/1?mode=reply")
        self.assertEqual((f["to"], f["subject"]), ("Max Mustermann <max@example.org>", "Re: Termin"))
        att = base64.b64encode(b"hello").decode()
        st, d = self.js("POST", "/api/send", {"acct": "work", "to": f["to"], "subject": f["subject"],
                                              "body": "Ja, passt.", "mode": "reply", "ref": "1",
                                              "atts": [{"name": "../notiz.txt", "data": att}]})
        self.assertEqual(st, 200, d)
        self.assertEqual(len(self.smtp.sent), 1)
        self.assertEqual(self.ntfy.published, [])  # no approval request
        msg = message_from_bytes(self.smtp.sent[0][2])
        self.assertEqual(msg["In-Reply-To"], "<t1@example.org>")
        self.assertEqual([p.get_filename() for p in msg.walk() if p.get_filename()], ["notiz.txt"])
        self.assertEqual(len(self.imap.folders["Sent"]["msgs"]), 1)
        self.assertIn("\\Answered", self.imap.flags("INBOX")[1])
        _, out = self.mg("log")
        self.assertIn("sent ui", out)
        st, d = self.js("POST", "/api/send", {"to": "a@example.org", "subject": "x", "body": "y"}, csrf=False)
        self.assertEqual(st, 403)
        self.assertEqual(len(self.smtp.sent), 1)

    def test_forward_with_attachments(self):
        self.login()
        st, f = self.js("GET", "/api/compose/1?mode=forward")
        self.assertEqual(f["subject"], "Fwd: Termin")
        self.js("POST", "/api/send", {"to": "erika@example.org", "subject": f["subject"], "body": "fyi",
                                      "mode": "forward", "ref": "1"})
        msg = message_from_bytes(self.smtp.sent[0][2])
        self.assertEqual([p.get_filename() for p in msg.walk() if p.get_filename()], ["rechnung.pdf"])

    def test_approvals(self):
        mime, rcpts = compose.build(self.cfg.account(), ["max@example.org"], "Vom Agenten", "Hallo")
        approve.create_draft(self.cfg, self.store, self.cfg.account(), mime, rcpts)
        self.assertEqual(self.js("POST", "/api/drafts/d1", {"do": "send"})[0], 401)
        self.login()
        st, d = self.js("GET", "/api/drafts")
        self.assertEqual(d[0]["id"], "d1")
        self.assertIn("Subject: Vom Agenten", d[0]["preview"])
        self.assertEqual(self.js("POST", "/api/drafts/d1", {"do": "send"}, csrf=False)[0], 403)
        st, d = self.js("POST", "/api/drafts/d1", {"do": "send"})
        self.assertEqual(st, 200, d)
        self.assertEqual(len(self.smtp.sent), 1)
        self.assertEqual(self.js("POST", "/api/drafts/d1", {"do": "send"})[0], 400)  # not pending any more

    def test_sync_event(self):
        self.login()
        seq = self.app.events.seq
        self.imap.add("INBOX", make_mail("Neu", "frisch"))
        self.assertEqual(self.app.sync(), 1)
        _, evs = self.app.events.wait(seq, 1)
        self.assertEqual(evs[-1]["t"], "sync")
        self.assertEqual(evs[-1]["new"], 1)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/events", headers={"Host": f"127.0.0.1:{self.port}", "Cookie": self.cookie})
        r = c.getresponse()
        self.assertEqual(r.headers["Content-Type"], "text/event-stream")
        self.assertEqual(r.fp.readline(), b": hello\n")
        self.app.events.publish({"t": "drafts", "n": 0})
        r.fp.readline()
        self.assertEqual(json.loads(r.fp.readline()[6:]), {"t": "drafts", "n": 0})
        c.close()


class ReadOnlyUiTest(UiTest):
    passphrase = False

    def test_read_only(self):
        st, d = self.js("GET", "/api/state")
        self.assertTrue(d["readonly"])
        self.assertFalse(d["need_login"])
        self.assertEqual(self.js("GET", "/api/list")[0], 200)
        self.assertEqual(self.js("POST", "/api/login", {"passphrase": ""})[0], 403)
        for path, body in (("/api/act", {"op": "trash", "ids": ["1"]}), ("/api/send", {"to": "a@example.org"}),
                           ("/api/drafts/d1", {"do": "send"})):
            st, d = self.js("POST", path, body)
            self.assertEqual(st, 403)
            self.assertIn("read-only", d["error"])
        self.assertEqual(self.smtp.sent, [])
        self.assertEqual(len(self.imap.folders["INBOX"]["msgs"]), 2)

    # the write tests of the parent class do not apply without a passphrase
    test_login_required = test_csrf_host_origin_and_expiry = test_list_msg_html_att = None
    test_archive_trash_move = test_send_reply_without_queue = test_forward_with_attachments = None
    test_approvals = test_sync_event = None


class BindTest(Env):
    def test_loopback_only(self):
        with self.assertRaises(ValueError):
            webui.server(self.cfg, self.dir / "mail.db", "0.0.0.0", 0)


if __name__ == "__main__":
    unittest.main()
