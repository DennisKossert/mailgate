"""SMTP delivery of approved drafts."""
from __future__ import annotations

import smtplib
import ssl

from .config import Account
from .plugin import assert_not_plugin


def connect(acct: Account) -> smtplib.SMTP:
    """Open an authenticated SMTP connection (465 SSL or 587 STARTTLS)."""
    assert_not_plugin("open an SMTP connection")
    s = acct.smtp
    if s.security == "ssl":
        conn: smtplib.SMTP = smtplib.SMTP_SSL(s.host, s.port, context=ssl.create_default_context(), timeout=60)
    else:
        conn = smtplib.SMTP(s.host, s.port, timeout=60)
        if s.security == "starttls":
            conn.starttls(context=ssl.create_default_context())
    conn.ehlo()
    conn.login(acct.user, acct.password())
    return conn


def send(acct: Account, mime: bytes, rcpts: list[str]) -> None:
    """Send the exact bytes; raises on any refused recipient."""
    assert_not_plugin("send mail over SMTP")
    conn = connect(acct)
    try:
        refused = conn.sendmail(acct.email, rcpts, mime)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    finally:
        try:
            conn.quit()
        except Exception:
            pass
