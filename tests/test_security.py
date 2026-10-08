"""Attempts to get around the approval queue and other safety rules. All must fail."""
import json
import os
import stat
import unittest
import urllib.request
from email.message import EmailMessage
from unittest import mock

from mailgate import approve, migrate, plugin, smtpsend, syncer, webui
from mailgate.config import load

from .helpers import Env, make_mail

EVIL = '''api_version = 1

def setup(mg):
    @mg.rule_action("evil")
    def evil(msg, arg, rule):
        from mailgate import approve, smtpsend
        cfg = mg._reg.cfg
        if arg == "smtp":
            smtpsend.send(cfg.account(), b"Subject: x\\r\\n\\r\\nx", ["victim@example.net"])
        elif arg == "draft_then_send":
            ref = mg.draft(["victim@example.net"], "exfil", "data")
            approve.send_draft(cfg, mg._store(), int(ref[1:], 36), via="plugin")
        elif arg == "token":
            approve.handle_command(cfg, mg._store(), "approve d1 whatever")
        elif arg == "ui":
            approve.send_now(cfg, mg._store(), cfg.account(), b"Subject: x\\r\\n\\r\\nx", ["victim@example.net"])
        elif arg == "draft":
            return mg.draft(["victim@example.net"], "only a draft", "x")
        return "done"
'''


class BypassTest(Env):
    def setUp(self) -> None:
        super().setUp()
        syncer.log = plugin._log = lambda msg: None
        (self.dir / "plugins").mkdir(mode=0o700)
        (self.dir / "plugins" / "evil.py").write_text(EVIL)
        plugin.pin("evil")

    def run_action(self, arg: str, approval: str = "") -> object:
        if approval:
            self.write_config(approval=approval)
        with open(self.dir / "config.toml", "a") as f:
            f.write(f'\n[plugins]\nenabled = ["evil"]\n\n[[rules]]\nname = "x"\nsubject = "."\naction = "evil:{arg}"\n')
        cfg = load()
        reg = plugin.load(cfg, self.dir / "mail.db")
        self.imap.add("INBOX", make_mail("Hallo", "x", msgid=f"<{arg}@example.org>"))
        self.mg("sync")
        from mailgate import sortrules
        return sortrules.apply(cfg, self.store, self.store.q("SELECT * FROM msgs"), reg=reg)

    def test_plugin_cannot_send(self):
        for arg in ("smtp", "draft_then_send", "token", "ui"):
            with self.subTest(arg=arg):
                self.run_action(arg)
                self.assertEqual(self.smtp.sent, [], arg)
                self.tearDown()
                self.setUp()

    def test_plugin_drafts_wait_even_in_auto_mode(self):
        lines = self.run_action("draft", approval='mode = "auto"')
        self.assertIn("d1", lines[0])
        self.assertEqual(self.store.draft(1)["status"], "pending")
        self.assertEqual(self.smtp.sent, [])
        self.assertEqual(self.ntfy.published[-1]["title"], "mailgate d1: approve sending?")

    def test_guard_direct(self):
        token = plugin._active.set("x")
        try:
            with self.assertRaises(plugin.PluginSendError):
                smtpsend.connect(self.cfg.account())
            with self.assertRaises(plugin.PluginSendError):
                approve.send_draft(self.cfg, self.store, 1, via="x")
        finally:
            plugin._active.reset(token)

    def test_run_hook_env_and_argv(self):
        out = self.dir / "hook.json"
        script = self.dir / "hook.py"
        script.write_text("import json, os, sys\nd = json.load(sys.stdin)\n"
                          f"json.dump({{'argv': sys.argv[1:], 'env': dict(os.environ), 'subject': d['subject']}}, "
                          f"open({str(out)!r}, 'w'))\n")
        import sys
        with open(self.dir / "config.toml", "a") as f:
            f.write(f'\n[[rules]]\nname = "hook"\nsubject = "."\naction = "run:{sys.executable} {script} fixed"\n')
        self.imap.add("INBOX", make_mail("$(touch /tmp/pwned); `id`", "x"))
        self.mg("sync")
        from mailgate import sortrules
        sortrules.apply(load(), self.store, self.store.q("SELECT * FROM msgs"))
        d = json.loads(out.read_text())
        self.assertEqual(d["argv"], ["fixed"])  # mail content never in argv
        self.assertEqual(d["subject"], "$(touch /tmp/pwned); `id`")
        self.assertNotIn("MG_TEST_PW", d["env"])  # password_env value not passed on
        self.assertNotIn("MAILGATE_CONFIG", d["env"])
        self.assertEqual(d["env"]["MG_SUBJECT"], "$(touch /tmp/pwned); `id`")


class AttachmentSafetyTest(Env):
    def test_path_traversal_and_dedupe(self):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = "billing@example.net", "jane@example.com", "Rechnung"
        m.set_content("anbei")
        m.add_attachment(b"%PDF evil", maintype="application", subtype="pdf", filename="../../../../tmp/evil.pdf")
        m.add_attachment(b"MZ", maintype="application", subtype="octet-stream", filename="run.exe")
        self.imap.add("INBOX", m.as_bytes())
        self.imap.add("INBOX", m.as_bytes())  # same content again
        target = self.dir / "Belege"
        with open(self.dir / "config.toml", "a") as f:
            f.write(f'\n[plugins]\nenabled = ["attachments"]\n\n[[rules]]\nname = "r"\nsubject = "rechnung"\n'
                    f'action = "save_attachments:{target}"\nsave_types = ["pdf"]\n')
        self.mg("sync")
        cfg = load()
        from mailgate import sortrules
        reg = plugin.load(cfg, self.dir / "mail.db")
        sortrules.apply(cfg, self.store, self.store.q("SELECT * FROM msgs"), reg=reg)
        files = sorted(p.name for p in target.iterdir())
        self.assertEqual(len(files), 1)  # pdf only, saved once
        self.assertTrue(files[0].endswith("_evil.pdf") and "/" not in files[0])
        self.assertEqual(stat.S_IMODE((target / files[0]).stat().st_mode), 0o600)
        self.assertFalse(os.path.exists("/tmp/evil.pdf") and open("/tmp/evil.pdf", "rb").read() == b"%PDF evil")


class UnsubscribeSafetyTest(Env):
    def mail(self, lu: str, post: str = "", frm: str = "News <news@example.com>") -> None:
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = frm, "jane@example.com", "News"
        m["List-Unsubscribe"] = lu
        if post:
            m["List-Unsubscribe-Post"] = post
        m.set_content("x")
        self.imap.add("INBOX", m.as_bytes())

    def setUp(self) -> None:
        super().setUp()
        with open(self.dir / "config.toml", "a") as f:
            f.write('\n[plugins]\nenabled = ["unsubscribe"]\n')
        self.cfg = load()
        plugin._log = lambda msg: None

    def reg(self):
        return plugin.load(self.cfg, self.dir / "mail.db")

    def test_one_click_only_https_and_only_on_request(self):
        self.mail("<http://track.example.com/u?id=1>, <https://news.example.com/unsub?id=1>",
                  "List-Unsubscribe=One-Click")
        calls = []

        class R:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass
        with mock.patch.object(urllib.request, "urlopen", lambda req, timeout: calls.append(req) or R()):
            self.mg("sync")
            reg = self.reg()
            view = next(v for v in reg.ui_views if v.id == "newsletters")
            items = reg.call(reg.apis["unsubscribe"], view.fn, {})["items"]
            self.assertEqual(items[0]["key"], "news@example.com")
            self.assertEqual(calls, [])  # listing never contacts anything
            act = view.actions[0]
            res = reg.call(reg.apis["unsubscribe"], act.fn, ["news@example.com"], None)
        self.assertIn("one-click", res)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].full_url, "https://news.example.com/unsub?id=1")
        self.assertEqual(calls[0].get_method(), "POST")
        self.assertEqual(calls[0].data, b"List-Unsubscribe=One-Click")

    def test_link_only_is_never_fetched_and_mailto_is_a_draft(self):
        self.mail("<https://news.example.com/page>")
        with mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("fetched")):
            self.mg("sync")
            reg = self.reg()
            act = next(v for v in reg.ui_views if v.id == "newsletters").actions[0]
            res = reg.call(reg.apis["unsubscribe"], act.fn, ["news@example.com"], None)
        self.assertIn("open this page", res)
        self.write_config(approval='mode = "auto"')
        self.mail("<mailto:leave@example.com?subject=unsubscribe>", frm="Other <other@example.com>")
        with open(self.dir / "config.toml", "a") as f:
            f.write('\n[plugins]\nenabled = ["unsubscribe"]\n')
        self.cfg = load()
        self.mg("sync")
        reg = self.reg()
        act = next(v for v in reg.ui_views if v.id == "newsletters").actions[0]
        res = reg.call(reg.apis["unsubscribe"], act.fn, ["other@example.com"], None)
        self.assertIn("waiting for approval", res)
        self.assertEqual(self.smtp.sent, [])  # never auto-sent, even in auto mode

    def test_cli_run_needs_tty(self):
        self.mail("<https://news.example.com/u>", "List-Unsubscribe=One-Click")
        self.mg("sync")
        code, _ = self.mg("unsub", "run", "news@example.com")
        self.assertNotEqual(code, 0)
        self.assertIn("interactive terminal", self.last_err)


class FilesTest(Env):
    def test_modes(self):
        db = self.dir / "new" / "mail.db"
        from mailgate.store import Store
        Store(db)
        self.assertEqual(stat.S_IMODE(db.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(db.parent.stat().st_mode), 0o700)
        os.chmod(self.dir / "config.toml", 0o644)
        from mailgate.config import insecure_paths
        self.assertTrue(any("config.toml" in w for w in insecure_paths()))
        _, out = self.mg("doctor")
        self.assertIn("chmod 600", out)

    def test_sanitizer_attribute_tricks(self):
        doc, _ = webui.sanitize('<a "onclick=alert(1)" href="https://x.example">a</a>'
                                '<img src="x" \x00LINK\x00="1"><a class="\x00LINK\x00" href="https://y.example">b</a>')
        self.assertNotIn("onclick", doc)
        self.assertNotIn("\x00", doc)
        self.assertIn('<a class="LINK" href="https://y.example"', doc)


class MigrateSafetyTest(Env):
    def test_secrets_refused_without_crypto(self):
        with mock.patch.object(migrate, "have_crypto", lambda: False):
            code, _ = self.mg("export", "-o", str(self.dir / "x.mgx"), "--with-secrets")
            self.assertNotEqual(code, 0)
            self.assertIn("refusing to write passwords unencrypted", self.last_err)
            self.assertFalse((self.dir / "x.mgx").exists())
            code, _ = self.mg("export", "--pair")
            self.assertNotEqual(code, 0)
        code, out = self.mg("export", "-o", str(self.dir / "plain.mgx"))
        self.assertEqual(code, 0, self.last_err)
        text = (self.dir / "plain.mgx").read_text()
        self.assertNotIn("secret", json.loads(text).get("secrets", {}))
        self.assertNotIn('"secrets"', text)
        self.assertEqual(stat.S_IMODE((self.dir / "plain.mgx").stat().st_mode), 0o600)


@unittest.skipUnless(migrate.have_crypto(), "cryptography not installed")
class PairingTest(Env):
    def test_wrong_code_lockout_and_single_use(self):
        data = migrate.bundle(self.cfg, self.store, with_secrets=True)
        self.assertEqual(data["secrets"]["accounts"]["work"], "secret")
        p = migrate.Pairing(data, host="127.0.0.1", port=0)
        self.assertNotIn("secret", json.dumps(p.env))  # encrypted on the wire
        p.start(ttl=30)
        wrong = "0000-0000-0000" if p.code != "0000-0000-0000" else "1111-1111-1111"
        with self.assertRaises(migrate.MigrateError):
            migrate.fetch_pair(f"{wrong}@127.0.0.1:{p.port}")
        got = migrate.fetch_pair(f"mgpair://127.0.0.1:{p.port}/{p.code}")
        self.assertEqual(got["secrets"]["accounts"]["work"], "secret")
        p.done.wait(3)
        self.assertEqual(p.state, "done")

    def test_lockout(self):
        p = migrate.Pairing(migrate.bundle(self.cfg, self.store), host="127.0.0.1", port=0)
        p.start(ttl=30)
        for _ in range(migrate.PAIR_MAX_FAILS):
            try:
                migrate.fetch_pair(f"0000-0000-000{_}@127.0.0.1:{p.port}")
            except Exception:
                pass
        p.done.wait(3)
        self.assertEqual(p.state, "locked")
        with self.assertRaises(Exception):
            migrate.fetch_pair(f"{p.code}@127.0.0.1:{p.port}")  # even the right code is useless now
