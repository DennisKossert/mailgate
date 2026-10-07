"""Shared test setup: temp config/db pointing at fake servers."""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone
from pathlib import Path

from mailgate import cli
from mailgate.config import load
from mailgate.store import Store

from .fakes import FakeIMAP, FakeNtfy, FakeSMTP


def make_mail(subject: str, body: str, frm: str = "Max Mustermann <max@example.org>",
              to: str = "Jane Doe <jane@example.com>", msgid: str = "", irt: str = "", refs: str = "",
              date: datetime | None = None, html: str | None = None, attach: bytes | None = None) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, to, subject
    m["Date"] = format_datetime(date or datetime(2026, 10, 5, 14, 2, tzinfo=timezone.utc))
    if msgid:
        m["Message-ID"] = msgid
    if irt:
        m["In-Reply-To"] = irt
        m["References"] = refs or irt
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    if attach is not None:
        m.add_attachment(attach, maintype="application", subtype="pdf", filename="rechnung.pdf")
    return m.as_bytes()


class Env(unittest.TestCase):
    """Starts fake IMAP/SMTP/ntfy and a temp config + db for each test."""
    ntfy_enabled = True

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.imap, self.smtp, self.ntfy = FakeIMAP(), FakeSMTP(), FakeNtfy()
        self.write_config()
        self.env = {"MAILGATE_CONFIG": str(self.dir / "config.toml"), "MAILGATE_DB": str(self.dir / "mail.db"),
                    "MG_TEST_PW": "secret"}
        self.old_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.cfg = load()
        self.store = Store(self.dir / "mail.db")

    def write_config(self, approval: str = "", rules: str = "", acct: str = "") -> None:
        """(Re)write the test config; extra TOML lines go into [approval], [approval.rules], [accounts.work]."""
        ntfy = (f'[approval.ntfy]\nserver = "{self.ntfy.url}"\ntopic = "mg-test-out"\n'
                f'reply_topic = "mg-test-reply"\napprove_label = "Senden"\nreject_label = "Verwerfen"\n'
                if self.ntfy_enabled else "")
        rules = f"[approval.rules]\n{rules}\n" if rules else ""
        (self.dir / "config.toml").write_text(f'''
[accounts.work]
email = "jane@example.com"
name = "Jane Doe"
imap_host = "127.0.0.1"
imap_port = {self.imap.port}
imap_security = "plain"
smtp_host = "127.0.0.1"
smtp_port = {self.smtp.port}
smtp_security = "plain"
password_env = "MG_TEST_PW"
folders = ["INBOX"]
sent_folder = "Sent"
signature = "Jane"
{acct}

[approval]
expiry_hours = 48
{approval}
{rules}{ntfy}''')
        if "MAILGATE_CONFIG" in os.environ and hasattr(self, "cfg"):
            self.cfg = load()

    def tearDown(self) -> None:
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.imap.stop()
        self.smtp.stop()
        self.ntfy.stop()
        self.tmp.cleanup()

    def mg(self, *args: str) -> tuple[int, str]:
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(args))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
                err.write(str(e.code))
        self.last_err = err.getvalue()
        return code, buf.getvalue()
