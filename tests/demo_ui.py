"""Demo of `mg ui` with fake servers and invented example.com mail (used for docs/ui.png).

    python -m tests.demo_ui [--port 8766] [--auto-login]

Nothing touches your real config or mailboxes: config, cache and passphrase live in a temp
directory. The demo passphrase is "demo-passphrase". --auto-login injects a session cookie
into every request so a headless browser can take a screenshot; it exists only in this script.
"""
from __future__ import annotations

import argparse
import base64
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from mailgate import approve, compose, imapsync, webui
from mailgate.config import load
from mailgate.store import Store

from .fakes import FakeIMAP, FakeSMTP
from .helpers import make_mail

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkqPtfDwAEgwG8Bo3uZQAAAABJRU5ErkJggg==")
NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)


def html_mail(subject: str, frm: str, when: datetime, html: str, text: str, msgid: str) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "Jane Doe <jane@example.com>", subject
    m["Date"], m["Message-ID"] = format_datetime(when), msgid
    m["List-Id"] = "Example News <news.example.com>"
    m["List-Unsubscribe"] = "<https://news.example.com/unsubscribe?u=123>"
    m["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    m["Authentication-Results"] = "mx.example.com; spf=pass; dkim=pass header.d=example.com; dmarc=pass"
    m.set_content(text)
    m.add_alternative(html, subtype="html")
    m.get_payload()[1].add_related(PNG, maintype="image", subtype="png", cid="<logo@example.com>")
    return m.as_bytes()


def seed(imap: FakeIMAP) -> None:
    ago = lambda **kw: NOW - timedelta(**kw)
    imap.folders.update({n: {"uv": 1, "next": 1, "msgs": []} for n in ("Archive", "Trash", "Newsletter")})
    imap.add("INBOX", make_mail("Plans for the garden house", "Hi Jane,\n\nthe carpenter can come next "
             "Tuesday at 9. Could you send me the final drawing until Monday?\n\nThanks,\nMax",
             msgid="<g1@example.org>", date=ago(days=3)), "\\Seen")
    imap.add("INBOX", make_mail("Re: Plans for the garden house", "Sure, I will send it on Sunday evening.\n\n"
             "Jane", frm="Jane Doe <jane@example.com>", to="Max Mustermann <max@example.org>",
             msgid="<g2@example.com>", irt="<g1@example.org>", date=ago(days=2, hours=20)), "\\Seen \\Answered")
    imap.add("INBOX", make_mail("Re: Plans for the garden house", "Got it, looks good. One question: is the "
             "window on the north side 80 or 100 cm wide? The carpenter wants to order the frame today.\n\n"
             "Max\n\nOn Sun, 5 Oct 2026 at 18:10, Jane Doe wrote:\n> Sure, I will send it on Sunday evening.",
             msgid="<g3@example.org>", irt="<g2@example.com>", refs="<g1@example.org> <g2@example.com>",
             date=ago(minutes=25)))
    imap.add("INBOX", make_mail("Invoice 2026-1043", "Dear customer,\n\nplease find attached your invoice "
             "for October.\n\nExample Hosting", frm="Example Hosting <billing@example.net>",
             msgid="<inv@example.net>", date=ago(hours=3), attach=b"%PDF-1.4 demo" * 40))
    imap.add("INBOX", html_mail("Your October newsletter", "Example News <news@example.com>", ago(hours=6),
             '<div style="max-width:560px;margin:auto;font-family:Georgia,serif">'
             '<img src="cid:logo@example.com" width="40" height="40" alt="">'
             '<h2>Autumn notes</h2><p>Three short reads for a rainy weekend, a recipe for apple cake '
             'and the dates for the winter market.</p>'
             '<img src="https://tracker.example.com/open.gif?u=123" width="1" height="1" alt="">'
             '<p><a href="https://news.example.com/october">Read online</a></p></div>',
             "Autumn notes. Three short reads for a rainy weekend.", "<nl10@news.example.com>"))
    imap.add("INBOX", make_mail("Photos from Sunday", "Here are the photos from the walk. The one at the lake "
             "came out really well!\n\nErika", frm="Erika Musterfrau <erika@example.org>",
             msgid="<p1@example.org>", date=ago(days=1, hours=2)), "\\Seen \\Flagged")
    imap.add("INBOX", make_mail("Club meeting on Thursday", "Hello everyone,\n\nthe next meeting is on "
             "Thursday at 19:00 in the small hall. Agenda: budget 2027, summer trip.\n\nTom",
             frm="Tom Example <tom@example.org>", msgid="<c1@example.org>", date=ago(days=1, hours=8)), "\\Seen")
    imap.add("INBOX", make_mail("Your parcel is on its way", "Your parcel will arrive tomorrow between 10 and 14.",
             frm="Example Parcel <noreply@parcel.example.com>", msgid="<pk@example.com>", date=ago(days=4)),
             "\\Seen")
    phish = EmailMessage()
    phish["From"], phish["To"] = "PayPal Service <service@paypal-konto.example.net>", "jane@example.com"
    phish["Subject"], phish["Date"] = "Your account has been limited", format_datetime(ago(hours=9))
    phish["Authentication-Results"] = "mx.example.com; spf=fail smtp.mailfrom=example.net; dkim=none; dmarc=fail"
    phish.set_content("Please confirm your account.")
    phish.add_alternative('<p>Dear customer,</p><p>please confirm your account within 24 hours:</p>'
                          '<p><a href="https://login.example.net/confirm?utm_source=mail">https://www.paypal.com/'
                          'account</a></p>', subtype="html")
    imap.add("INBOX", phish.as_bytes())
    imap.add("Archive", make_mail("Tax documents 2025", "All documents are in the shared folder now.",
             frm="Max Mustermann <max@example.org>", msgid="<tx@example.org>", date=ago(days=40)), "\\Seen")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--auto-login", action="store_true")
    ap.add_argument("--tui", action="store_true", help="run mg tui instead of the web UI")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="mg-demo-"))
    imap, smtp = FakeIMAP(), FakeSMTP()
    seed(imap)
    (tmp / "config.toml").write_text(f'''
[accounts.home]
email = "jane@example.com"
name = "Jane Doe"
imap_host = "127.0.0.1"
imap_port = {imap.port}
imap_security = "plain"
smtp_host = "127.0.0.1"
smtp_port = {smtp.port}
smtp_security = "plain"
password_env = "MG_DEMO_PW"
folders = ["INBOX", "Archive"]
signature = "Jane"

[ui]
lang = "en"

[plugins]
enabled = ["auth", "linkclean", "dedupe", "unsubscribe", "followup"]
''')
    os.environ.update(MAILGATE_CONFIG=str(tmp / "config.toml"), MAILGATE_DB=str(tmp / "mail.db"),
                      MG_DEMO_PW="secret")
    cfg = load()
    store = Store(tmp / "mail.db")
    imapsync.sync_account(store, cfg.account(), log=lambda s: None)
    mime, rcpts = compose.build(cfg.account(), ["Max Mustermann <max@example.org>"], "Re: Plans for the garden house",
                                "Hi Max,\n\nthe north window is 100 cm wide, the drawing is updated.\n\nJane",
                                in_reply_to="<g3@example.org>")
    approve.create_draft(cfg, store, cfg.account(), mime, rcpts, via="agent")
    if a.tui:
        from mailgate import tui
        tui.run(load(), tmp / "mail.db")
        return
    webui.set_passphrase("demo-passphrase", webui.pass_path())
    srv, app = webui.server(cfg, tmp / "mail.db", "127.0.0.1", a.port)
    if a.auto_login:
        token, _ = app.login("demo-passphrase")
        handler = srv.RequestHandlerClass

        class AutoLogin(handler):
            def parse_request(self) -> bool:
                ok = super().parse_request()
                if ok and "Cookie" not in self.headers:
                    self.headers["Cookie"] = f"{webui.COOKIE}={token}"
                return ok
        srv.RequestHandlerClass = AutoLogin
    app.last_sync = int(NOW.timestamp())
    print(f"demo on http://localhost:{srv.server_address[1]}/  passphrase: demo-passphrase  data: {tmp}", flush=True)
    threading.Thread(target=app.drafts_loop, args=(threading.Event(),), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
