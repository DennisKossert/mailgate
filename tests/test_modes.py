import time
import unittest

from mailgate import approve, daemon
from mailgate.config import ConfigError, parse

from .helpers import Env, make_mail


class ModesTest(Env):
    def setUp(self) -> None:
        super().setUp()
        daemon.log = lambda msg: None

    def draft(self, to: str = "max@example.org", *extra: str) -> tuple[int, str]:
        (self.dir / "b.txt").write_text("Hallo,\n\nkurze Info.\n")
        return self.mg("draft", "--to", to, "--subject", "Info", "--body-file", str(self.dir / "b.txt"), *extra)

    def audit(self, action: str) -> list:
        return self.store.q("SELECT via, detail FROM audit WHERE action=?", (action,))

    def test_manual_is_default(self):
        _, out = self.draft()
        self.assertIn("waiting for human approval", out)
        self.assertEqual(self.smtp.sent, [])

    def test_auto_send_immediately(self):
        self.write_config(approval='mode = "auto"')
        code, out = self.draft()
        self.assertEqual(code, 0, self.last_err)
        self.assertIn("d1 sent automatically (approval mode auto)", out)
        self.assertEqual(len(self.smtp.sent), 1)
        self.assertEqual(self.smtp.sent[0][2], bytes(self.store.draft(1)["mime"]))
        via, detail = self.audit("sent")[0]
        self.assertEqual(via, "auto")
        self.assertIn("mode=auto", detail)
        self.assertIn("sent automatically", self.ntfy.published[-1]["message"])
        _, out = self.mg("doctor")
        self.assertIn("WARN work: approval mode auto, drafts are SENT WITHOUT human approval", out)
        _, out = self.mg("config-check")
        self.assertIn("WARN work", out)

    def test_undo_window_cancel_and_send(self):
        self.write_config(approval='mode = "auto"\nundo_seconds = 30')
        _, out = self.draft()
        self.assertIn("d1 queued, auto-send in 30s by mg daemon", out)
        p = self.ntfy.published[-1]
        self.assertEqual([a["label"] for a in p["actions"]], ["Stop"])
        token = p["actions"][0]["body"].split()[2]
        self.assertEqual(approve.process_due(self.cfg, self.store), [])  # window not over
        self.assertEqual(approve.handle_command(self.cfg, self.store, f"reject d1 {token}"), "d1 rejected")
        self.draft()  # d2: cancelled from the CLI
        _, out = self.mg("queue")
        self.assertIn("d2", out)
        self.assertIn("auto-send", out)
        self.assertEqual(self.mg("cancel", "d2")[1].strip(), "d2 cancelled")
        self.draft()  # d3: window passes, daemon sends
        self.store.db.execute("UPDATE drafts SET send_at=? WHERE id=3", (int(time.time()) - 1,))
        res = approve.process_due(self.cfg, self.store)
        self.assertEqual(len(res), 1)
        self.assertIn("d3 sent", res[0])
        self.assertEqual(len(self.smtp.sent), 1)
        self.assertEqual([r["status"] for r in self.store.q("SELECT status FROM drafts ORDER BY id")],
                         ["rejected", "cancelled", "sent"])

    def test_rules_pass_and_fallback(self):
        self.write_config(approval='mode = "rules"', rules='allow_to = ["*@example.org"]\ndeny_attachments = true')
        _, out = self.draft("max@example.org")
        self.assertIn("sent automatically (approval mode rules)", out)
        self.assertEqual(self.audit("sent")[0][0], "auto-rules")
        _, out = self.draft("Bob <bob@example.net>")
        self.assertIn("waiting for human approval", out)
        self.assertIn("rule failed: allow_to: bob@example.net", out)
        self.assertIn("Not sent automatically: rule failed", self.ntfy.published[-1]["message"])
        self.assertEqual([a["label"] for a in self.ntfy.published[-1]["actions"]], ["Senden", "Verwerfen"])
        (self.dir / "a.pdf").write_bytes(b"%PDF")
        _, out = self.draft("max@example.org", "--attach", str(self.dir / "a.pdf"))
        self.assertIn("deny_attachments: has attachments", out)
        self.assertEqual(len(self.smtp.sent), 1)
        self.assertEqual(len(self.audit("manual_fallback")), 2)

    def test_rules_reply_only_and_accounts(self):
        self.write_config(approval='mode = "rules"',
                          rules='allow_to = ["*@example.org"]\nreply_only = true\nallow_accounts = ["home"]')
        _, out = self.draft()
        self.assertIn("allow_accounts: work not allowed", out)
        self.assertIn("reply_only: not a reply", out)
        self.write_config(approval='mode = "rules"', rules='allow_to = ["*@example.org"]\nreply_only = true')
        self.imap.add("INBOX", make_mail("Frage", "Geht das?", msgid="<q@example.org>"))
        self.mg("sync")
        (self.dir / "r.txt").write_text("Ja.")
        _, out = self.mg("reply", "1", "--body-file", str(self.dir / "r.txt"))
        self.assertIn("sent automatically", out)
        self.assertEqual(len(self.smtp.sent), 1)

    def test_rate_limit_fallback(self):
        self.write_config(approval='mode = "auto"\nmax_per_hour = 2')
        self.draft()
        self.draft()
        _, out = self.draft()
        self.assertIn("rate limit reached (2/h for work)", out)
        self.assertEqual(len(self.smtp.sent), 2)
        self.assertEqual(self.store.draft(3)["status"], "pending")
        self.assertIn("rate limit", self.ntfy.published[-1]["message"])

    def test_rate_limit_at_send_time(self):
        self.write_config(approval='mode = "auto"\nmax_per_hour = 1\nundo_seconds = 5')
        self.draft()
        self.store.db.execute("UPDATE drafts SET send_at=0")
        self.assertIn("d1 sent", approve.process_due(self.cfg, self.store)[0])
        self.draft()  # counts the sent one: falls back at creation
        self.assertEqual(self.store.draft(2)["status"], "pending")

    def test_per_account_override(self):
        self.write_config(approval='mode = "auto"', acct='approval_mode = "manual"')
        _, out = self.draft()
        self.assertIn("waiting for human approval", out)
        self.assertEqual(self.smtp.sent, [])
        _, out = self.mg("doctor")
        self.assertIn("ok   work: approval mode manual", out)

    def test_bad_mode(self):
        with self.assertRaises(ConfigError):
            parse({"approval": {"mode": "yolo"}, "accounts": {"a": {
                "email": "a@example.com", "imap_host": "h", "smtp_host": "h", "password_env": "X"}}})


if __name__ == "__main__":
    unittest.main()
