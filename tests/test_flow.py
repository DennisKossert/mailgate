import http.client
import re
import threading
import time
import unittest
import urllib.parse
from email import message_from_bytes

from mailgate import approve, daemon

from .helpers import Env, make_mail


class FlowTest(Env):
    def setUp(self) -> None:
        super().setUp()
        daemon.log = lambda msg: None

    def draft(self, body: str = "Hallo Max,\n\nDonnerstag passt.\n\nGruß Jane") -> int:
        (self.dir / "body.txt").write_text(body)
        code, out = self.mg("draft", "--to", "Max Mustermann <max@example.org>", "--cc", "team@example.org",
                            "--subject", "Termin", "--body-file", str(self.dir / "body.txt"))
        self.assertEqual(code, 0, self.last_err)
        self.assertRegex(out, r"^d1 queued, waiting for human approval")
        self.assertIn("To: Max Mustermann <max@example.org>", out)
        return 1

    def token(self) -> str:
        body = self.ntfy.published[-1]["actions"][0]["body"]
        return body.split()[2]

    def test_draft_ntfy_approve_send_append(self):
        did = self.draft()
        self.assertEqual(self.smtp.sent, [])
        p = self.ntfy.published[0]
        self.assertEqual(p["topic"], "mg-test-out")
        self.assertIn("Donnerstag passt.", p["message"])
        self.assertIn("Subject: Termin", p["message"])
        labels = [a["label"] for a in p["actions"]]
        self.assertEqual(labels, ["Senden", "Verwerfen"])
        self.assertTrue(p["actions"][0]["url"].endswith("/mg-test-reply"))
        token = self.token()
        self.assertNotIn(token, str(self.store.draft(did)["token_hash"]))
        res = approve.handle_command(self.cfg, self.store, f"approve d1 {token}")
        self.assertIn("d1 sent", res)
        frm, rcpts, data = self.smtp.sent[0]
        self.assertEqual(frm, "jane@example.com")
        self.assertEqual(rcpts, ["max@example.org", "team@example.org"])
        self.assertEqual(data, bytes(self.store.draft(did)["mime"]))  # byte-identical
        uid, flags, copy = self.imap.folders["Sent"]["msgs"][0]
        self.assertEqual(flags, "\\Seen")
        self.assertEqual(copy, data)
        msg = message_from_bytes(data)
        self.assertEqual(msg["Subject"], "Termin")
        self.assertIn("--=20\r\nJane", data.decode())  # signature, QP-encoded
        self.assertIsNone(msg["Bcc"])
        res = approve.handle_command(self.cfg, self.store, f"approve d1 {token}")
        self.assertIn("not pending", res)
        self.assertEqual(len(self.smtp.sent), 1)
        _, out = self.mg("log")
        self.assertRegex(out, r"d1 sent ntfy \| .*Termin \| appended to Sent")

    def test_wrong_token_and_reject(self):
        self.draft()
        res = approve.handle_command(self.cfg, self.store, "approve d1 forged-token")
        self.assertIn("invalid token", res)
        self.assertEqual(self.store.draft(1)["status"], "pending")
        self.assertEqual(approve.handle_command(self.cfg, self.store, "hello"), "ignored: not a command")
        res = approve.handle_command(self.cfg, self.store, f"reject d1 {self.token()}")
        self.assertEqual(res, "d1 rejected")
        self.assertEqual(self.smtp.sent, [])
        _, out = self.mg("log")
        self.assertIn("bad_token", out)

    def test_expiry(self):
        self.draft()
        self.store.db.execute("UPDATE drafts SET expires=? WHERE id=1", (int(time.time()) - 1,))
        res = approve.handle_command(self.cfg, self.store, f"approve d1 {self.token()}")
        self.assertIn("expired", res)
        self.assertEqual(self.smtp.sent, [])
        _, out = self.mg("queue")
        self.assertEqual(out.strip(), "queue empty")

    def test_tampered_draft_not_sent(self):
        self.draft()
        mime = bytes(self.store.draft(1)["mime"]).replace(b"max@example.org", b"evil@example.net")
        self.store.db.execute("UPDATE drafts SET mime=? WHERE id=1", (mime,))
        with self.assertRaises(approve.ApprovalError):
            approve.send_draft(self.cfg, self.store, 1, via="test")
        self.assertEqual(self.smtp.sent, [])
        self.assertEqual(self.store.draft(1)["status"], "failed")

    def test_reply_threading(self):
        self.imap.add("INBOX", make_mail("AW: Angebot", "Wie besprochen.", msgid="<o2@example.org>",
                                         irt="<o1@example.com>", to="jane@example.com, Erika <erika@example.org>"))
        self.mg("sync")
        (self.dir / "r.txt").write_text("Danke!")
        code, out = self.mg("reply", "1", "--body-file", str(self.dir / "r.txt"), "--all")
        self.assertEqual(code, 0, self.last_err)
        msg = message_from_bytes(bytes(self.store.draft(1)["mime"]))
        self.assertEqual(msg["Subject"], "AW: Angebot")
        self.assertEqual(msg["In-Reply-To"], "<o2@example.org>")
        self.assertEqual(msg["References"], "<o1@example.com> <o2@example.org>")
        self.assertEqual(msg["To"], "Max Mustermann <max@example.org>")
        self.assertEqual(msg["Cc"], "Erika <erika@example.org>")
        self.assertEqual(self.store.draft(1)["reply_msg"], 1)

    def test_cancel_and_cli_approve_needs_tty(self):
        self.draft()
        code, _ = self.mg("approve", "d1")
        self.assertNotEqual(code, 0)
        self.assertIn("interactive terminal", self.last_err)
        code, out = self.mg("cancel", "d1")
        self.assertEqual(out.strip(), "d1 cancelled")
        self.assertEqual(self.smtp.sent, [])

    def test_ntfy_stream(self):
        self.draft()
        self.ntfy.post("mg-test-reply", "approve d1 wrong")
        self.ntfy.post("mg-test-reply", f"approve d1 {self.token()}")
        daemon.ntfy_stream(self.cfg, self.store, threading.Event(), timeout=5)
        self.assertEqual(len(self.smtp.sent), 1)
        self.assertEqual(self.store.kv_get("ntfy_since"), "m1")

    def test_web_ui(self):
        self.draft()
        srv = daemon.web_server(self.cfg, self.dir / "mail.db", "127.0.0.1:0")
        threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True).start()
        port = srv.server_address[1]
        try:
            c = http.client.HTTPConnection("127.0.0.1", port)
            c.request("GET", "/")
            page = c.getresponse().read().decode()
            self.assertIn("Donnerstag passt.", page)
            csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)

            def post(token: str, host: str = f"127.0.0.1:{port}") -> int:
                c = http.client.HTTPConnection("127.0.0.1", port)
                body = urllib.parse.urlencode({"csrf": token, "id": "d1", "do": "send"})
                c.request("POST", "/act", body, {"Content-Type": "application/x-www-form-urlencoded", "Host": host})
                return c.getresponse().status
            self.assertEqual(post("wrong"), 403)
            self.assertEqual(post(csrf, host="evil.example.com"), 403)
            self.assertEqual(self.smtp.sent, [])
            self.assertEqual(post(csrf), 303)
            self.assertEqual(len(self.smtp.sent), 1)
        finally:
            srv.shutdown()
            srv.server_close()
        with self.assertRaises(ValueError):
            daemon.web_server(self.cfg, self.dir / "mail.db", "0.0.0.0:8765")


class NoNtfyTest(Env):
    ntfy_enabled = False

    def test_draft_without_ntfy(self):
        (self.dir / "b.txt").write_text("x")
        code, out = self.mg("draft", "--to", "a@example.org", "--subject", "s", "--body-file", str(self.dir / "b.txt"))
        self.assertEqual(code, 0)
        self.assertEqual(self.ntfy.published, [])
        _, out = self.mg("queue")
        self.assertRegex(out, r"^d1 .* work -> a@example.org \| s \(expires ")


class ConfigTest(unittest.TestCase):
    def test_rejects_plaintext_and_remote_plain(self):
        from mailgate.config import ConfigError, parse
        base = {"email": "a@example.com", "imap_host": "imap.example.com", "smtp_host": "smtp.example.com",
                "password_env": "X"}
        with self.assertRaises(ConfigError):
            parse({"accounts": {"a": dict(base, password="hunter2")}})
        with self.assertRaises(ConfigError):
            parse({"accounts": {"a": dict(base, imap_security="plain")}})
        cfg = parse({"accounts": {"a": base}})
        self.assertEqual((cfg.account().imap.port, cfg.account().smtp.port), (993, 465))


if __name__ == "__main__":
    unittest.main()
