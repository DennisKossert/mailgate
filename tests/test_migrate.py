import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from mailgate import migrate, syncer
from mailgate.config import load
from mailgate.store import Store

from .helpers import Env, make_mail


class MigrateTest(Env):
    def seed(self) -> None:
        self.imap.add("INBOX", make_mail("Eins", "a", msgid="<1@example.org>"))
        self.mg("sync")
        self.mg("new", "phone", "--backlog")
        (self.dir / "b.txt").write_text("Hallo")
        self.mg("draft", "--to", "max@example.org", "--subject", "Offen", "--body-file", str(self.dir / "b.txt"))
        self.store.db.execute("INSERT INTO plugin_data VALUES('followup','r','k1','{\"due\": 1}', 1)")

    def move_to_new_device(self) -> Store:
        """Point config + db at an empty 'device'."""
        new = self.dir / "device2"
        new.mkdir()
        os.environ["MAILGATE_CONFIG"] = str(new / "config.toml")
        os.environ["MAILGATE_DB"] = str(new / "mail.db")
        return Store(new / "mail.db")

    def test_plain_roundtrip(self):
        self.seed()
        code, out = self.mg("export", "-o", str(self.dir / "x.mgx"))
        self.assertEqual(code, 0, self.last_err)
        self.assertIn("Not encrypted", out)
        store2 = self.move_to_new_device()
        code, out = self.mg("import", str(self.dir / "x.mgx"))
        self.assertEqual(code, 0, self.last_err)
        self.assertIn("1 watchers, 1 drafts, 1 plugin records", out)
        self.assertEqual(load().account().email, "jane@example.com")
        self.assertEqual(store2.draft(1)["subject"], "Offen")
        self.assertEqual(store2.one("SELECT COUNT(*) FROM msgs")[0], 0)  # no mail cache
        self.mg("sync")
        self.imap.add("INBOX", make_mail("Zwei", "b", msgid="<2@example.org>"))
        self.mg("sync")
        _, out = self.mg("new", "phone")
        self.assertEqual([ln.split("|")[1].strip() for ln in out.splitlines()], ["Zwei"])  # watcher kept its state
        code, _ = self.mg("import", str(self.dir / "x.mgx"))
        self.assertNotEqual(code, 0)  # existing config is not overwritten silently
        code, _ = self.mg("import", str(self.dir / "x.mgx"), "--force")
        self.assertEqual(code, 0)
        self.assertTrue(list((self.dir / "device2").glob("config.toml.bak-*")))

    @unittest.skipUnless(migrate.have_crypto(), "cryptography not installed")
    def test_encrypted_with_secrets(self):
        self.seed()
        code, out = self.mg("export", "-o", str(self.dir / "s.mgx"), "--with-secrets")
        self.assertEqual(code, 0, self.last_err)
        code_word = out.split("code (keep it apart from the file): ")[1].split()[0]
        raw = (self.dir / "s.mgx").read_text()
        self.assertNotIn("secret", raw.replace("secrets", ""))
        self.assertNotIn("jane@example.com", raw)
        with self.assertRaises(migrate.MigrateError):
            migrate.read_file(self.dir / "s.mgx", "0000-0000-0000")
        self.move_to_new_device()
        with mock.patch.object(migrate, "store_secret", lambda acct, pw: f"echo-from-keyring {acct}"):
            code, out = self.mg("import", str(self.dir / "s.mgx"), "--code", code_word.lower())
        self.assertEqual(code, 0, self.last_err)
        self.assertIn("password stored in the system keyring", out)
        text = Path(os.environ["MAILGATE_CONFIG"]).read_text()
        self.assertIn('password_cmd = "echo-from-keyring work"', text)
        self.assertNotIn("password_env", text)

    def test_code_format(self):
        c = migrate.new_code()
        self.assertRegex(c, r"^[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}$")
        self.assertEqual(migrate.norm_code(c.lower().replace("-", " ")), c.replace("-", ""))
        self.assertEqual(migrate.parse_target("mgpair://192.0.2.5:9000/ABCD-EFGH-JKMN"), ("192.0.2.5", 9000, "ABCD-EFGH-JKMN"))
        self.assertEqual(migrate.parse_target("ABCD-EFGH-JKMN@host"), ("host", migrate.PAIR_PORT, "ABCD-EFGH-JKMN"))

    @unittest.skipUnless(shutil.which("zbarimg"), "zbarimg not installed")
    def test_qr_decodes(self):
        m = migrate.qr_matrix("mgpair://192.0.2.5:8767/ABCD-EFGH-JKMN")
        n, s = len(m), 6
        pix = [[255] * ((n + 8) * s) for _ in range((n + 8) * s)]
        for y, row in enumerate(m):
            for x, on in enumerate(row):
                if on:
                    for dy in range(s):
                        for dx in range(s):
                            pix[(y + 4) * s + dy][(x + 4) * s + dx] = 0
        w = len(pix)
        f = self.dir / "qr.pgm"
        f.write_bytes(f"P5 {w} {w} 255\n".encode() + bytes(v for row in pix for v in row))
        out = subprocess.run(["zbarimg", "-q", "--raw", str(f)], capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "mgpair://192.0.2.5:8767/ABCD-EFGH-JKMN")
        self.assertIn("▀", migrate.qr_terminal("x"))


class HeadlessDaemonTest(Env):
    def test_daemon_sync_applies_rules(self):
        syncer.log = lambda msg: None
        self.imap.folders["Newsletter"] = {"uv": 1, "next": 1, "msgs": []}
        with open(self.dir / "config.toml", "a") as f:
            f.write('\n[[rules]]\nname = "nl"\nsubject = "newsletter"\naction = "move:Newsletter"\n')
        cfg = load()
        s = syncer.Syncer(cfg, self.dir / "mail.db", cfg.sync_minutes)
        s.sync()
        self.imap.add("INBOX", make_mail("Newsletter Oktober", "x"))
        s.sync()
        self.assertEqual(len(self.imap.folders["Newsletter"]["msgs"]), 1)
        self.assertEqual(self.imap.folders["INBOX"]["msgs"], [])
        code, _ = self.mg("daemon", "--help")
        self.assertEqual(code, 0)
