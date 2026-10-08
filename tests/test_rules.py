from email.message import EmailMessage

from mailgate import sortrules
from mailgate.config import ConfigError, parse

from .helpers import Env, make_mail

RULES = '''
[[rules]]
name = "Newsletter"
list_id = 'news\\.example\\.com'
action = ["mark_read", "move:Newsletter"]

[[rules]]
name = "Rechnungen"
from = 'billing@example\\.net'
header = { "X-Priority" = "^1" }
action = "flag"

[[rules]]
name = "Wichtig"
subject = "dringend"
action = "flag"
'''


def listmail(subject: str) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "News <news@example.com>", "jane@example.com", subject
    m["List-Id"] = "Example News <news.example.com>"
    m.set_content("Neuigkeiten")
    return m.as_bytes()


class RulesTest(Env):
    def setUp(self) -> None:
        super().setUp()
        self.imap.folders["Newsletter"] = {"uv": 1, "next": 1, "msgs": []}
        with open(self.dir / "config.toml", "a") as f:
            f.write(RULES)
        from mailgate.config import load
        self.cfg = load()

    def seed(self) -> None:
        self.imap.add("INBOX", listmail("Oktober-Ausgabe"))
        rech = make_mail("Rechnung", "Anbei.", frm="Billing <billing@example.net>").replace(
            b"Subject:", b"X-Priority: 1\r\nSubject:", 1)
        self.imap.add("INBOX", rech)
        self.imap.add("INBOX", make_mail("Ganz normal", "Hallo"))
        self.imap.add("INBOX", make_mail("Dringend: Rückruf", "Bitte"))

    def test_match_and_dry_run(self):
        self.seed()
        self.mg("sync")
        code, out = self.mg("rules", "test", "-n", "10")
        self.assertEqual(code, 0, self.last_err)
        self.assertIn("Oktober-Ausgabe -> Newsletter: mark_read, move:Newsletter", out)
        self.assertIn("Rechnung -> Rechnungen: flag", out)
        self.assertIn("Dringend: Rückruf -> Wichtig: flag", out)
        self.assertNotIn("Ganz normal", out)
        self.assertIn("3 of the last 10 mails match (dry run", out)
        self.assertEqual(len(self.imap.folders["INBOX"]["msgs"]), 4)  # nothing changed
        self.assertFalse([c for c in self.imap.commands if "STORE" in c or "MOVE" in c])

    def test_apply_new_only(self):
        self.imap.add("INBOX", listmail("Alt"))
        self.mg("sync")
        self.assertEqual(sortrules.apply_new(self.cfg, self.store), [])  # first call sets the start point
        self.seed()
        self.mg("sync")
        lines = sortrules.apply_new(self.cfg, self.store)
        self.assertEqual(len(lines), 3)
        inbox = {m[2].split(b"Subject: ")[1].split(b"\r\n")[0].split(b"\n")[0]: m[1]
                 for m in self.imap.folders["INBOX"]["msgs"]}
        self.assertIn(b"Alt", inbox)  # old mail untouched
        self.assertNotIn(b"Oktober-Ausgabe", inbox)
        moved = self.imap.folders["Newsletter"]["msgs"]
        self.assertEqual(len(moved), 1)
        self.assertIn("\\Seen", moved[0][1])
        self.assertIn("\\Flagged", inbox[b"Rechnung"])
        self.assertEqual(sortrules.apply_new(self.cfg, self.store), [])

    def test_header_must_match(self):
        self.imap.add("INBOX", make_mail("Rechnung ohne Prio", "x", frm="billing@example.net"))
        self.mg("sync")
        self.assertEqual(sortrules.plan(self.cfg, sortrules.recent(self.store, 5)), [])

    def test_config_validation(self):
        base = {"accounts": {"a": {"email": "a@example.com", "imap_host": "i.example.com",
                                   "smtp_host": "s.example.com", "password_env": "X"}}}
        with self.assertRaises(ConfigError):
            parse(dict(base, rules=[{"from": "x", "action": "no such!"}]))
        with self.assertRaises(ConfigError):
            parse(dict(base, rules=[{"action": "flag"}]))
        with self.assertRaises(ConfigError):
            parse(dict(base, rules=[{"subject": "(", "action": "flag"}]))
        self.assertEqual(len(parse(dict(base, rules=[{"to": "x", "action": "move:Y"}])).sort_rules), 1)

    def test_init_example_rules_parse(self):
        import tomllib
        from mailgate.config import example
        lines = example().splitlines()
        start = lines.index("# [[rules]]")
        text = "\n".join(ln[2:] if i >= start and (ln.startswith("# [[") or " = " in ln) else ln
                         for i, ln in enumerate(lines))
        rules = parse(tomllib.loads(text)).sort_rules
        self.assertEqual([r.name for r in rules], ["Newsletters", "Receipts", "Invoices"])
        self.assertEqual(rules[1].extra["save_types"], ["pdf"])
