import json
import os
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from mailgate import plugin, syncer
from mailgate.config import load

from .helpers import Env, make_mail

HELLO = '''"""Test plugin."""
api_version = 1

def setup(mg):
    @mg.on_message_synced
    def tag(msg):
        return {"len": len(msg.subject)}

    @mg.on_render
    def render(msg):
        return {"lines": [f"Hello: {msg.meta()['len']}"], "badges": [{"text": "hi", "tone": "ok"}]}

    @mg.rule_condition("subject_len")
    def cond(msg, value):
        return len(msg.subject) == value

    @mg.rule_action("note")
    def note(msg, arg, rule):
        mg.data_set("notes", msg.id, arg)
        return "noted"

    @mg.on_new_mail
    def new(msg):
        mg.emit("seen", {"id": msg.id})
        raise RuntimeError("plugin bug")  # must not break anything

    def args(p):
        p.add_argument("word")

    @mg.command("hello", "say hello", args)
    def hello(a):
        print("hello " + a.word + " " + str(len(mg.data_list("notes"))))
'''


class PluginTest(Env):
    def setUp(self) -> None:
        super().setUp()
        syncer.log = plugin._log = lambda msg: None
        (self.dir / "plugins").mkdir(mode=0o700)
        (self.dir / "plugins" / "hello.py").write_text(HELLO)

    def enable(self, *names: str, rules: str = "") -> None:
        for n in names:
            plugin.pin(n)
        with open(self.dir / "config.toml", "a") as f:
            f.write(f"\n[plugins]\nenabled = {json.dumps(list(names))}\n{rules}")
        self.cfg = load()

    def test_user_plugin_needs_pin_and_unchanged_file(self):
        with open(self.dir / "config.toml", "a") as f:
            f.write('\n[plugins]\nenabled = ["hello"]\n')
        reg = plugin.load(load(), self.dir / "mail.db", quiet=True)
        self.assertIn("not pinned", reg.loaded["hello"].error)  # never auto-loaded from the dir
        plugin.pin("hello")
        reg = plugin.load(load(), self.dir / "mail.db", quiet=True)
        self.assertEqual(reg.loaded["hello"].error, "")
        with open(self.dir / "plugins" / "hello.py", "a") as f:
            f.write("\n# changed\n")
        reg = plugin.load(load(), self.dir / "mail.db", quiet=True)
        self.assertIn("changed since it was enabled", reg.loaded["hello"].error)
        self.assertEqual(reg.hooks, {})

    def test_refuses_writable_plugin_files(self):
        os.chmod(self.dir / "plugins" / "hello.py", 0o666)
        with self.assertRaises(plugin.PluginError):
            plugin.pin("hello")
        os.chmod(self.dir / "plugins" / "hello.py", 0o600)
        plugin.pin("hello")
        os.chmod(self.dir / "plugins", 0o777)
        with open(self.dir / "config.toml", "a") as f:
            f.write('\n[plugins]\nenabled = ["hello"]\n')
        reg = plugin.load(load(), self.dir / "mail.db", quiet=True)
        self.assertIn("writable by group/others", reg.loaded["hello"].error)
        os.chmod(self.dir / "plugins", 0o700)

    def test_api_version(self):
        (self.dir / "plugins" / "future.py").write_text("api_version = 99\ndef setup(mg): pass\n")
        plugin.pin("future")
        reg = plugin.load(self.cfg, self.dir / "mail.db", names=["future"], quiet=True)
        self.assertIn("api_version 99", reg.loaded["future"].error)

    def test_hooks_rules_commands_events(self):
        self.mg("sync")  # sets the starting points
        self.enable("hello", rules='\n[[rules]]\nname = "short"\nsubject_len = 4\naction = "note:short"\n')
        self.imap.add("INBOX", make_mail("Kurz", "x", msgid="<k@example.org>"))
        self.imap.add("INBOX", make_mail("Viel länger", "y", msgid="<l@example.org>"))
        self.mg("sync")  # mg sync: metadata hook only, no rules
        _, out = self.mg("read", "1")
        self.assertIn("Hello: 4", out)
        code, out = self.mg("hello", "you")
        self.assertEqual((code, out.strip()), (0, "hello you 0"), self.last_err)
        self.imap.add("INBOX", make_mail("Klein", "z", msgid="<m@example.org>"))
        s = syncer.Syncer(self.cfg, self.dir / "mail.db", 2)
        s.sync()  # background sync: rules (plugin condition + action) and on_new_mail
        _, out = self.mg("rules", "test")
        self.assertIn("Kurz -> short: note:short", out)
        _, out = self.mg("hello", "again")
        self.assertEqual(out.strip(), "hello again 0")  # "Klein" has 5 letters, no match
        self.imap.add("INBOX", make_mail("Mini", "z", msgid="<n@example.org>"))
        s.sync()
        _, out = self.mg("hello", "again")
        self.assertEqual(out.strip(), "hello again 1")
        _, out = self.mg("events")
        types = [json.loads(ln)["type"] for ln in out.splitlines()]
        self.assertIn("new_mail", types)
        self.assertIn("plugin.hello.seen", types)
        self.assertIn("sync", types)
        self.assertEqual(self.mg("--help")[0], 0)

    def test_core_commands_cannot_be_replaced_and_ls_does_not_load_plugins(self):
        (self.dir / "plugins" / "evil.py").write_text(
            "api_version = 1\nimport sys\nsys.modules['mailgate_evil_loaded'] = True\n"
            "def setup(mg):\n    @mg.command('ls', 'fake ls')\n    def ls(a):\n        print('pwned')\n")
        self.enable("evil")
        import sys
        sys.modules.pop("mailgate_evil_loaded", None)
        _, out = self.mg("ls")
        self.assertNotIn("pwned", out)
        self.assertNotIn("mailgate_evil_loaded", sys.modules)

    def test_bundled_defaults(self):
        reg = plugin.load(self.cfg, self.dir / "mail.db")
        self.assertEqual(sorted(reg.loaded), ["auth", "dedupe", "linkclean"])
        self.assertTrue(all(not x.error for x in reg.loaded.values()))
        href, warn = reg.link("https://shop.example.com/a?utm_source=nl&id=3&fbclid=x", "www.paypal.com")
        self.assertEqual(href, "https://shop.example.com/a?id=3")
        self.assertEqual(warn, ["goes to shop.example.com"])
        items = [{"id": "1", "mi": "<a@x>"}, {"id": "2", "mi": "<a@x>"}, {"id": "3", "mi": ""}]
        self.assertEqual([i["id"] for i in reg.filter_list(items, {"kind": "unified"})], ["1", "3"])
        self.assertEqual(len(reg.filter_list(items, {"kind": "folder"})), 3)
        _, out = self.mg("plugins", "list")
        self.assertRegex(out, r"auth\s+enabled\s+bundled")
        self.assertRegex(out, r"followup\s+-\s+bundled")


class AuthPluginTest(Env):
    def test_badges_and_read_line(self):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = "PayPal Service <service@paypa1-secure.example.net>", "jane@example.com", "Konto"
        m["Authentication-Results"] = "mx.example.com; spf=fail smtp.mailfrom=x; dkim=none; dmarc=fail"
        m.set_content("Bitte bestätigen")
        m.add_alternative('<a href="https://evil.example.net/login">https://www.paypal.com/login</a>', subtype="html")
        self.imap.add("INBOX", m.as_bytes())
        ok = EmailMessage()
        ok["From"], ok["To"], ok["Subject"] = "Max <max@example.org>", "jane@example.com", "Hallo"
        ok["Authentication-Results"] = "mx.example.com; spf=pass; dkim=pass header.d=example.org; dmarc=pass"
        ok.set_content("Hi")
        self.imap.add("INBOX", ok.as_bytes())
        self.mg("sync")
        _, out = self.mg("read", "1")
        line = next(ln for ln in out.splitlines() if ln.startswith("Trust:"))
        self.assertIn("dmarc=fail", line)
        self.assertIn("name says Paypal, domain is example.net", line)
        self.assertIn("link text www.paypal.com goes to evil.example.net", line)
        _, out = self.mg("read", "2", "--json")
        self.assertEqual(json.loads(out)["pl"], ["Trust: dmarc=pass spf=pass dkim=pass"])
        reg = plugin.load(self.cfg, self.dir / "mail.db")
        r = reg.render(self.store.get(1))
        self.assertEqual(r["badges"][0]["tone"], "bad")
        self.assertTrue(r["banners"])


class SnoozeFollowupTest(Env):
    def test_remind_snooze(self):
        with open(self.dir / "config.toml", "a") as f:
            f.write('\n[plugins]\nenabled = ["followup", "dedupe"]\n')
        self.cfg = load()
        syncer.log = plugin._log = lambda msg: None
        self.imap.add("INBOX", make_mail("Angebot", "Was meinst du?", frm="Jane Doe <jane@example.com>",
                                         to="max@example.org", msgid="<o@example.com>"))
        self.imap.add("INBOX", make_mail("Später", "lesen", msgid="<s@example.org>"))
        self.mg("sync")
        code, out = self.mg("remind", "1", "--in", "1m", "--if-no-reply")
        self.assertEqual(code, 0, self.last_err)
        self.mg("remind", "1", "--in", "1m")
        code, out = self.mg("snooze", "2", "--until", "2d")
        self.assertIn("snoozed until", out)
        reg = plugin.load(self.cfg, self.dir / "mail.db")
        items = [{"id": "1", "mi": "<o@example.com>"}, {"id": "2", "mi": "<s@example.org>"}]
        self.assertEqual([i["id"] for i in reg.filter_list(items, {"kind": "unified"})], ["1"])
        self.store.db.execute("UPDATE plugin_data SET data=json_set(data, '$.due', 1) WHERE kind='r' "
                              "AND json_extract(data, '$.kind')='remind'")
        self.imap.add("INBOX", make_mail("Re: Angebot", "Passt.", msgid="<r@example.org>", irt="<o@example.com>",
                                         date=datetime.now(timezone.utc) + timedelta(minutes=1)))
        self.mg("sync")
        reg.tick()
        _, out = self.mg("remind", "--list")
        self.assertEqual(out.count("due"), 1)  # the if-no-reply one was answered
        self.assertIn("Follow up: Angebot", self.ntfy.published[-1]["message"])


class ExamplePluginTest(Env):
    def test_example_plugin(self):
        import shutil
        from pathlib import Path
        (self.dir / "plugins").mkdir(mode=0o700)
        shutil.copy(Path(__file__).parent.parent / "examples" / "hello_plugin.py", self.dir / "plugins" / "boss.py")
        os.chmod(self.dir / "plugins" / "boss.py", 0o600)
        plugin.pin("boss")
        with open(self.dir / "config.toml", "a") as f:
            f.write('\n[plugins]\nenabled = ["boss"]\n[plugins.boss]\naddress = "max@example.org"\n')
        self.imap.add("INBOX", make_mail("Status?", "x"))
        self.mg("sync")
        _, out = self.mg("read", "1")
        self.assertIn("Note: from your boss", out)
        _, out = self.mg("boss")
        self.assertEqual(out.strip(), "1 Status?")
