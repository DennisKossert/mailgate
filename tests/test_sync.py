import json
import re
import unittest
from datetime import datetime, timezone

from mailgate import imapsync

from .helpers import Env, make_mail

LINE = re.compile(r"^[0-9a-z]+\*?@? \d\d-\d\d \d\d:\d\d \w+/\S+ .{1,20} \| .{0,60}$")


class SyncTest(Env):
    def seed(self) -> None:
        self.imap.add("INBOX", make_mail("Termin Donnerstag", "Passt dir Donnerstag?", msgid="<t1@example.org>",
                                         date=datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)), "\\Seen")
        self.imap.add("INBOX", make_mail("Re: Termin Donnerstag", "Ja, passt.\n\nAm 01.10.2026 um 11:00 schrieb "
                                         "Max <max@example.org>:\n> Passt dir Donnerstag?",
                                         frm="Jane Doe <jane@example.com>", msgid="<t2@example.com>",
                                         irt="<t1@example.org>", date=datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)),
                      "\\Seen")
        self.imap.add("INBOX", make_mail("Rechnung Oktober", "Anbei die Rechnung.", frm="Billing <billing@example.net>",
                                         msgid="<r1@example.net>", attach=b"%PDF" * 50))

    def test_sync_ls_read(self):
        self.seed()
        code, out = self.mg("sync")
        self.assertEqual(code, 0, self.last_err)
        self.assertIn("work/INBOX +3", out)
        self.assertTrue(all(c.startswith(("CAPABILITY", "LOGIN", "EXAMINE", "UID", "LOGOUT"))
                            for c in self.imap.commands), self.imap.commands)  # read-only
        code, out = self.mg("ls")
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        for ln in lines:
            self.assertRegex(ln, LINE)
        self.assertTrue(lines[0].startswith("3*@ "), lines[0])
        self.assertIn("work/INBOX Billing | Rechnung Oktober", lines[0])
        _, out = self.mg("ls", "--unread")
        self.assertEqual(len(out.splitlines()), 1)
        _, out = self.mg("read", "3")
        self.assertIn("Subject: Rechnung Oktober", out)
        self.assertIn("Att: rechnung.pdf (200B)", out)
        self.assertTrue(out.rstrip().endswith("Anbei die Rechnung."))
        _, out = self.mg("read", "2", "--max", "5")
        self.assertIn("[...5 more chars, use --full]", out)
        _, out = self.mg("read", "2", "--json")
        d = json.loads(out)
        self.assertEqual(d["b"], "Ja, passt.")
        _, out = self.mg("ls", "--json", "-n", "1")
        self.assertEqual(json.loads(out)[0]["at"], 1)

    def test_incremental_flags_uidvalidity(self):
        self.seed()
        self.mg("sync")
        self.imap.add("INBOX", make_mail("Neu", "neue Mail", msgid="<n1@example.org>"))
        self.imap.folders["INBOX"]["msgs"][2][1] = "\\Seen"  # read elsewhere
        _, out = self.mg("sync")
        self.assertIn("+1", out)
        _, out = self.mg("stats")
        self.assertIn("work/INBOX 4 msgs, 1 unread", out)
        self.mg("sync")
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM msgs")[0], 4)
        del self.imap.folders["INBOX"]["msgs"][0]  # deleted on server
        self.mg("sync")
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM msgs")[0], 3)
        self.imap.folders["INBOX"]["uv"] = 2  # server reset
        _, out = self.mg("sync")
        self.assertIn("+3", out)
        self.assertEqual(self.store.one("SELECT COUNT(*) FROM msgs")[0], 3)

    def test_search_thread(self):
        self.seed()
        self.mg("sync")
        _, out = self.mg("search", "rechnung")
        self.assertEqual(len(out.splitlines()), 1)
        _, out = self.mg("search", "donnerstag", "--from", "jane")
        self.assertEqual(len(out.splitlines()), 1)
        _, out = self.mg("thread", "2")
        self.assertTrue(out.startswith("Thread: Termin Donnerstag (2 msgs)"), out)
        self.assertEqual(out.count("Passt dir Donnerstag?"), 1)  # quote stripped, no duplication
        self.store.fts = False
        rows = self.store.search("Rechnung")
        self.assertEqual(len(rows), 1)

    def test_watcher_dedup(self):
        self.seed()
        self.mg("sync")
        _, out = self.mg("new", "notify")
        self.assertEqual(out, "")  # first run starts at "now"
        self.imap.add("INBOX", make_mail("Wichtig: Vertrag", "Bitte unterschreiben.", msgid="<v@example.org>"))
        self.imap.add("INBOX", make_mail("Newsletter", "Angebote", msgid="<nl@example.org>"))
        self.mg("sync")
        _, out = self.mg("new", "notify", "--match", "vertrag")
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("Wichtig: Vertrag", out)
        _, out = self.mg("new", "notify")
        self.assertEqual(out, "")
        _, out = self.mg("new", "other", "--backlog")
        self.assertEqual(len(out.splitlines()), 5)

    def test_attachments_and_raw_cap(self):
        self.seed()
        self.cfg.max_raw_bytes = 10
        acct = self.cfg.account()
        imapsync.sync_account(self.store, acct, max_raw=10, log=lambda s: None)
        self.assertIsNone(self.store.get(3)["raw"])
        code, out = self.mg("att", "3", "--out", str(self.dir / "att"))
        self.assertEqual(code, 0, self.last_err)
        self.assertIn("rechnung.pdf (200 bytes)", out)
        self.assertEqual((self.dir / "att" / "rechnung.pdf").read_bytes(), b"%PDF" * 50)

    def test_utf7(self):
        self.assertEqual(imapsync.utf7("Entwürfe"), "Entw&APw-rfe")
        self.assertEqual(imapsync.utf7("A&B"), "A&-B")


if __name__ == "__main__":
    unittest.main()
