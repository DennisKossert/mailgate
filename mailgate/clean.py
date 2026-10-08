"""Parse raw mail and reduce it to short plain text for LLMs."""
from __future__ import annotations

import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

SKIP_TAGS = {"script", "style", "head", "title", "noscript", "template", "svg"}
PARA_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "blockquote", "hr"}
LINE_TAGS = {"div", "tr", "section", "article", "header", "footer", "center", "dd", "dt"}


_ESC_SEQ = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[PX^_][^\x1b]*(?:\x1b\\)?|.?)")
_CTRL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def term_safe(s: str | None, oneline: bool = False) -> str:
    """Remove terminal escape sequences, control characters and bidi overrides from mail text,
    so a subject or body can never move the cursor, retitle the window or fake other output."""
    s = _ESC_SEQ.sub("", s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = _CTRL.sub("", s)
    return " ".join(s.split()) if oneline else s


def short_url(url: str) -> str:
    """Keep short clean URLs, reduce long or tracking ones to their domain."""
    if len(url) <= 50 and "?" not in url and "%" not in url:
        return url
    host = urlsplit(url).netloc.lower().split("@")[-1].split(":")[0]
    return f"[link: {host.removeprefix('www.')}]"


class _HtmlText(HTMLParser):
    def __init__(self, drop_quotes: bool):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.quote = 0
        self.drop_quotes = drop_quotes
        self.href: str | None = None
        self.atext: list[str] = []

    def _emit(self, s: str) -> None:
        if self.skip or (self.drop_quotes and self.quote):
            return
        (self.atext if self.href is not None else self.out).append(s)

    def _nl(self) -> None:
        buf = self.atext if self.href is not None else self.out
        if buf and not buf[-1].endswith("\n"):
            self._emit("\n")

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag == "blockquote":
            self.quote += 1
        if tag == "br":
            self._emit("\n")
        elif tag == "li":
            self._emit("\n- ")
        elif tag in PARA_TAGS:
            self._emit("\n\n")
        elif tag in LINE_TAGS:
            self._nl()
        elif tag in ("td", "th"):
            self._emit(" ")
        elif tag == "a" and self.href is None:
            self.href = a.get("href") or ""
            self.atext = []

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag == "blockquote":
            self.quote = max(0, self.quote - 1)
        elif tag == "a" and self.href is not None:
            href, text = self.href, " ".join("".join(self.atext).split())
            self.href = None
            link = short_url(href) if href.startswith(("http://", "https://")) else ""
            if not text or not link:
                self._emit(text or link)
            elif text.rstrip("/") == href.rstrip("/"):
                self._emit(link)
            else:
                self._emit(f"{text} {link}" if link.startswith("[") else f"{text} ({link})")
        elif tag in PARA_TAGS:
            self._emit("\n\n")
        elif tag in LINE_TAGS:
            self._nl()

    def handle_data(self, data: str) -> None:
        self._emit(data)


def html_to_text(html: str, drop_quotes: bool = True) -> str:
    """Convert HTML to plain text; optionally drop <blockquote> content."""
    p = _HtmlText(drop_quotes)
    try:
        p.feed(html)
        p.close()
    except Exception:  # malformed markup: keep what we have
        pass
    return "".join(p.out)


_URL = re.compile(r"<?(https?://[^\s<>\"']+)>?")
_ZW = re.compile("[​‌‍⁠﻿­͏]")
_REPLY_HDR = re.compile("|".join([
    r"^On (?=[^\n]*\d)[^\n]{5,200}(?:\n[^\n]{0,120})?wrote:[ \t]*$",
    r"^Am (?=[^\n]*\d)[^\n]{5,200}(?:\n[^\n]{0,120})?schrieb[^\n]{0,120}:[ \t]*$",
    r"^Le (?=[^\n]*\d)[^\n]{5,200}a écrit ?:[ \t]*$",
    r"^-{2,} ?(?:Original Message|Ursprüngliche Nachricht|Originalnachricht|Original-Nachricht) ?-{2,}",
    r"^_{10,}[ \t]*\n+(?:From|Von): ",
    r"^(?:From|Von): [^\n]+\n(?:Sent|Gesendet|Date|Datum): ",
]), re.M | re.I)
_SIG = re.compile(r"^-- ?$", re.M)
_MOBILE = re.compile(r"^(?:Sent from my .*|Von meinem .*gesendet\.?|Gesendet von .*|"
                     r"Get Outlook for .*|Outlook für .* beziehen)$", re.M | re.I)
_DISCLAIMER = re.compile("|".join([
    r"if you (?:are not|have received) (?:the intended|this (?:e-?mail|message) in error)",
    r"intended (?:only )?for the (?:named )?(?:recipient|addressee)",
    r"this (?:e-?mail|message)(?: and any attachments?)? (?:is|are|may be|contains?) (?:strictly )?"
    r"(?:confidential|privileged)",
    r"diese (?:e-?mail|nachricht) (?:enthält|ist|kann) [^.]{0,40}vertraulich",
    r"nicht der (?:richtige|beabsichtigte) (?:adressat|empfänger)",
    r"irrtümlich erhalten",
    r"please consider the environment", r"denken sie an die umwelt",
    r"unsubscribe", r"abmelden", r"abbestellen", r"manage (?:your )?(?:email )?preferences",
    r"view (?:this|it) in (?:your )?browser", r"im browser (?:ansehen|anzeigen|öffnen)",
    r"registergericht", r"handelsregister", r"amtsgericht", r"ust-?id", r"steuernummer",
    r"geschäftsführer(?:in)?:", r"sitz der gesellschaft", r"registered office", r"company (?:no|number|registration)",
]), re.I)


def tidy(text: str) -> str:
    """Normalize whitespace and zero-width junk."""
    text = _ZW.sub("", text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " "))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def strip_quotes(text: str) -> str:
    """Remove reply headers with everything below them, and '>' quote lines."""
    m = _REPLY_HDR.search(text)
    if m:
        head = text[:m.start()]
        text = head if head.strip() else text[m.end():]  # bottom-posted reply: drop header only
    return "\n".join(ln for ln in text.split("\n") if not ln.lstrip().startswith(">"))


def strip_footer(text: str) -> str:
    """Remove signature, mobile taglines and legal/newsletter footer paragraphs."""
    m = _SIG.search(text)
    if m and text[:m.start()].strip():
        text = text[:m.start()]
    text = _MOBILE.sub("", text)
    paras = re.split(r"\n\s*\n", text)
    keep = [p for i, p in enumerate(paras) if i == 0 or len(p) > 1500 or not _DISCLAIMER.search(p)]
    return "\n\n".join(keep)


def clean(text: str) -> str:
    """Full cleaning pipeline for already-extracted plain text."""
    text = tidy(text)
    text = strip_footer(strip_quotes(text))
    text = _URL.sub(lambda m: short_url(m.group(1)), text)
    return tidy(text)


def _content(part) -> str:
    try:
        return part.get_content()
    except Exception:
        raw = part.get_payload(decode=True) or b""
        return raw.decode(part.get_content_charset() or "utf-8", "replace")


def body_text(msg: EmailMessage) -> tuple[str, str]:
    """Return (full_text, cleaned_text) from the best body part (plain, else HTML)."""
    for kind in ("plain", "html"):
        part = msg.get_body(preferencelist=(kind,))
        raw = _content(part) if part is not None else None
        if not isinstance(raw, str) or not raw.strip():
            continue
        if kind == "html":
            return tidy(html_to_text(raw, drop_quotes=False)), clean(html_to_text(raw, drop_quotes=True))
        return tidy(raw), clean(raw)
    return "", ""


def html_body(msg: EmailMessage) -> str | None:
    part = msg.get_body(preferencelist=("html",))
    if part is None or part.get_content_type() != "text/html":
        return None
    try:
        return part.get_content()
    except Exception:
        return (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")


def attachment_parts(msg: EmailMessage) -> list:
    """MIME parts that are real attachments (inline cid images are skipped)."""
    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = part.get_content_disposition()
        if not part.get_filename() and disp != "attachment":
            continue
        if disp == "inline" and part.get_content_maintype() == "image" and part.get("Content-ID"):
            continue
        out.append(part)
    return out


def attachments(msg: EmailMessage) -> list[tuple[str, int]]:
    """List (filename, size) of real attachments, skipping inline images."""
    return [(p.get_filename() or "unnamed", len(p.get_payload(decode=True) or b"")) for p in attachment_parts(msg)]


def _hdr(msg: EmailMessage, name: str) -> str:
    try:
        v = msg.get(name)
        return " ".join(str(v).split()) if v is not None else ""
    except Exception:
        return ""


def parse_message(raw: bytes) -> dict:
    """Parse raw RFC822 bytes into the fields mailgate stores."""
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    name, addr = parseaddr(_hdr(msg, "From"))
    try:
        date = int(parsedate_to_datetime(_hdr(msg, "Date")).timestamp())
    except Exception:
        date = None
    full, body = body_text(msg)
    return {
        "msgid": _hdr(msg, "Message-ID"), "irt": _hdr(msg, "In-Reply-To"),
        "refs": _hdr(msg, "References"), "reply_to": _hdr(msg, "Reply-To"),
        "date": date, "from_name": name or addr, "from_addr": addr.lower(),
        "to_addr": _hdr(msg, "To"), "cc": _hdr(msg, "Cc"), "subject": _hdr(msg, "Subject"),
        "atts": attachments(msg), "full": full, "body": body,
    }


def human_size(n: int) -> str:
    """1234567 -> '1.2M'."""
    for unit in ("B", "K", "M"):
        if n < 1024 or unit == "M":
            return f"{n}{unit}" if unit == "B" else f"{n:.1f}{unit}".replace(".0", "")
        n /= 1024
    return str(n)
