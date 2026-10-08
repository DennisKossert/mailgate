"""Build outgoing MIME messages for drafts and replies."""
from __future__ import annotations

import mimetypes
import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, formatdate, getaddresses, make_msgid
from pathlib import Path
from sqlite3 import Row

from .config import Account

REPLY_RE = re.compile(r"^\s*(re|aw|antw|sv|vs|odp)\s*:", re.I)


def addr_list(values: list[str] | None) -> list[str]:
    """Flatten '-to a@x,b@y' style values into a list of address strings."""
    out = []
    for v in values or []:
        out += [formataddr(p) if p[0] else p[1] for p in getaddresses([v]) if "@" in p[1]]
    return out


def build(acct: Account, to: list[str], subject: str, body: str, cc: list[str] | None = None,
          bcc: list[str] | None = None, in_reply_to: str = "", references: str = "",
          attach: list[str] | None = None, attach_data: list[tuple[str, bytes]] | None = None,
          signature: bool = True) -> tuple[bytes, list[str]]:
    """Render the final MIME bytes (CRLF) and the envelope recipients."""
    if not to:
        raise ValueError("no recipient")
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = formataddr((acct.display_name, acct.email))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=acct.email.split("@")[1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references + " " + in_reply_to).strip()
    text = body.rstrip() + "\n"
    if acct.signature and signature:
        text += "\n-- \n" + acct.signature.rstrip() + "\n"
    msg.set_content(text, cte="quoted-printable")
    files = [(Path(f).name, Path(f).read_bytes()) for f in attach or []] + list(attach_data or [])
    for name, data in files:
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        maint, sub = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maint, subtype=sub, filename=name)
    raw = msg.as_bytes(policy=policy.SMTP)
    if not raw.endswith(b"\r\n"):
        raw += b"\r\n"
    rcpts = [a for _, a in getaddresses(to + (cc or []) + (bcc or []))]
    return raw, rcpts


def reply_fields(acct: Account, orig: Row, reply_all: bool = False) -> dict:
    """To/Cc/Subject/threading headers for a reply to a cached message."""
    name = orig["from_name"] if orig["from_name"] != orig["from_addr"] else ""
    target = orig["reply_to"] or formataddr((name, orig["from_addr"]))
    to = addr_list([target])
    if orig["from_addr"].lower() == acct.email.lower() and addr_list([orig["to_addr"] or ""]):
        to = addr_list([orig["to_addr"]])  # replying to my own sent mail: write to its recipients
    cc: list[str] = []
    if reply_all:
        me = acct.email.lower()
        taken = {a.lower() for _, a in getaddresses(to)} | {me}
        for name, a in getaddresses([orig["to_addr"] or "", orig["cc"] or ""]):
            if "@" in a and a.lower() not in taken:
                taken.add(a.lower())
                cc.append(formataddr((name, a)) if name else a)
    subj = orig["subject"] or ""
    if not REPLY_RE.match(subj):
        subj = f"{acct.reply_prefix} {subj}".strip()
    return {"to": to, "cc": cc, "subject": subj, "in_reply_to": orig["msgid"] or "",
            "references": orig["refs"] or ""}


def preview(mime: bytes, limit: int | None = None) -> str:
    """Human-readable From/To/Cc/Subject + body of a rendered message."""
    msg = BytesParser(policy=policy.default).parsebytes(mime)
    lines = [f"{h}: {msg[h]}" for h in ("From", "To", "Cc", "Subject") if msg[h]]
    atts = [p.get_filename() for p in msg.iter_attachments()]
    if atts:
        lines.append("Attachments: " + ", ".join(a or "unnamed" for a in atts))
    part = msg.get_body(preferencelist=("plain",))
    body = part.get_content() if part else ""
    if limit is not None and len(body.encode()) > limit:
        cut = body.encode()[:limit].decode(errors="ignore")
        body = cut + f"\n[... {len(body.encode()) - len(cut.encode())} more bytes not shown]"
    return "\n".join(lines) + "\n\n" + body.rstrip()
