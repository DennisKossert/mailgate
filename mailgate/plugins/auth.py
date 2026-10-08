"""Authenticity hints: SPF/DKIM/DMARC badge, look-alike sender names and misleading links.

Hints for a human (and an agent) reading the mail, not a spam filter. Adds a badge and
warnings in the UI, a "Trust:" line in `mg read`, a warning next to links whose text shows
another domain than the real target, and the rule conditions `auth = "fail"|"pass"|"none"`
and `suspicious = true`.
"""
from __future__ import annotations

import re
import time
from email.message import EmailMessage
from email.utils import getaddresses
from html.parser import HTMLParser
from urllib.parse import urlsplit


# brand word in the display name -> domains that may legitimately use it
BRANDS = {
    "paypal": ("paypal.com", "paypal.de"), "amazon": ("amazon.de", "amazon.com", "amazon.co.uk", "amazonses.com"),
    "apple": ("apple.com", "icloud.com"), "icloud": ("apple.com", "icloud.com"),
    "microsoft": ("microsoft.com", "outlook.com", "live.com", "office.com"), "outlook": ("microsoft.com", "outlook.com"),
    "google": ("google.com", "gmail.com", "youtube.com"), "netflix": ("netflix.com",), "ebay": ("ebay.de", "ebay.com"),
    "dhl": ("dhl.de", "dhl.com"), "deutsche post": ("deutschepost.de", "dhl.de"), "dpd": ("dpd.de", "dpd.com"),
    "hermes": ("myhermes.de", "hermesworld.com"), "ups": ("ups.com",), "fedex": ("fedex.com",),
    "sparkasse": ("sparkasse.de",), "volksbank": ("vr.de", "volksbank.de"), "commerzbank": ("commerzbank.de",),
    "deutsche bank": ("db.com", "deutsche-bank.de"), "postbank": ("postbank.de",), "ing": ("ing.de", "ing.com"),
    "dkb": ("dkb.de",), "n26": ("n26.com",), "comdirect": ("comdirect.de",), "telekom": ("telekom.de", "t-online.de"),
    "vodafone": ("vodafone.de", "vodafone.com"), "o2": ("o2online.de", "telefonica.de"), "1&1": ("1und1.de", "ionos.de"),
    "facebook": ("facebook.com", "facebookmail.com", "meta.com"), "instagram": ("instagram.com", "facebookmail.com"),
    "linkedin": ("linkedin.com",), "steam": ("steampowered.com", "steamcommunity.com"), "elster": ("elster.de",),
    "finanzamt": ("elster.de",), "visa": ("visa.com", "visa.de"), "mastercard": ("mastercard.com",),
}
TWO_LEVEL = {"co.uk", "org.uk", "ac.uk", "com.au", "co.jp", "co.nz", "com.br", "co.at", "or.at", "com.tr"}
DOMAIN_TEXT = re.compile(r"^\s*(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})(?:[/:?#]\S*)?\s*$", re.I)


def base_domain(host: str) -> str:
    """Registrable domain, roughly: mail.paypal.com -> paypal.com, a.b.co.uk -> b.co.uk."""
    parts = host.lower().strip(".").split("@")[-1].split(":")[0].split(".")
    n = 3 if ".".join(parts[-2:]) in TWO_LEVEL else 2
    return ".".join(parts[-n:])


def auth_results(msg: EmailMessage) -> dict[str, str]:
    """spf/dkim/dmarc results from the topmost Authentication-Results header (added by your provider)."""
    hdr = msg.get_all("Authentication-Results") or []
    if not hdr:
        return {}
    out: dict[str, str] = {}
    for method, result in re.findall(r"\b(spf|dkim|dmarc)=(\w+)", str(hdr[0]), re.I):
        method, result = method.lower(), result.lower()
        if out.get(method) != "pass":  # several DKIM signatures: one pass is enough
            out[method] = result
    return out


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.href: str | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href, self.text = dict(attrs).get("href") or "", []

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            self.links.append((self.href, " ".join("".join(self.text).split())))
            self.href = None

    def handle_data(self, data):
        if self.href is not None:
            self.text.append(data)


def link_mismatch(href: str, text: str) -> str | None:
    """Domain the link really goes to, if its text shows a different URL/domain; else None."""
    m = DOMAIN_TEXT.match(text or "")
    if not m or not href.lower().startswith(("http://", "https://")):
        return None
    real = urlsplit(href).hostname or ""
    return real if real and base_domain(real) != base_domain(m.group(1)) else None


def html_links(markup: str) -> list[tuple[str, str]]:
    p = _Links()
    try:
        p.feed(markup)
        p.close()
    except Exception:
        pass
    return p.links


def check(msg: EmailMessage, from_name: str, from_addr: str, html: str | None = None,
          contacts: dict[str, set[str]] | None = None) -> dict:
    """{'auth': {...}, 'verdict': pass|fail|none, 'warnings': [str]} for one message."""
    auth = auth_results(msg)
    warnings: list[str] = []
    domain = base_domain(from_addr.split("@")[-1]) if "@" in from_addr else ""
    name = (from_name or "").lower()
    if name and name != from_addr.lower():
        shown = [a.lower() for _, a in getaddresses([from_name]) if "@" in a]
        if shown and shown[0] != from_addr.lower():
            warnings.append(f"name shows {shown[0]}, sent from {from_addr}")
        for brand, domains in BRANDS.items():
            if re.search(rf"(?<![\w&]){re.escape(brand)}(?![\w&])", name) and domain not in domains:
                warnings.append(f"name says {brand.title()}, domain is {domain}")
                break
        known = (contacts or {}).get(name)
        if known and from_addr.lower() not in known and all(base_domain(a.split("@")[-1]) != domain for a in known):
            warnings.append(f"name of your contact {from_name}, but address {from_addr} is new")
    bad = [(t, real) for href, t in html_links(html or "") if (real := link_mismatch(href, t))]
    for text, real in bad[:3]:
        warnings.append(f"link text {DOMAIN_TEXT.match(text).group(1)} goes to {real}")
    if len(bad) > 3:
        warnings.append(f"{len(bad) - 3} more misleading links")
    vals = set(auth.values())
    if auth.get("dmarc") == "pass" or (auth.get("spf") == "pass" and auth.get("dkim") == "pass"):
        verdict = "pass"
    elif "fail" in vals or "permerror" in vals or auth.get("dmarc") in ("quarantine", "reject"):
        verdict = "fail"
        warnings.insert(0, "sender authentication failed (" + ", ".join(f"{k}={v}" for k, v in auth.items()) + ")")
    else:
        verdict = "none"
    return {"auth": auth, "verdict": verdict, "warnings": warnings}


def contacts(q) -> dict[str, set[str]]:
    """Display name (lower) -> addresses, from mail you sent and senders with at least two mails."""
    out: dict[str, set[str]] = {}
    for (to,) in q("SELECT to_addr FROM drafts WHERE status='sent'"):
        for n, a in getaddresses([to or ""]):
            if n and "@" in a:
                out.setdefault(n.lower(), set()).add(a.lower())
    for r in q("SELECT from_name, from_addr, COUNT(*) c FROM msgs WHERE from_name!=from_addr "
                     "GROUP BY from_name, from_addr HAVING c>=2"):
        out.setdefault(r["from_name"].lower(), set()).add(r["from_addr"])
    return out


def summary(res: dict) -> str:
    """One compact line for `mg read`, or '' when there is nothing to say."""
    auth = " ".join(f"{k}={res['auth'][k]}" for k in ("dmarc", "spf", "dkim") if k in res["auth"])
    parts = [auth] if auth else []
    parts += ["! " + w for w in res["warnings"]]
    return "; ".join(parts)



# ---- plugin -----------------------------------------------------------------------------------

api_version = 1
_cache: dict = {}


def setup(mg) -> None:
    def known() -> dict:
        if time.time() - _cache.get("t", 0) > 600:
            _cache.update(t=time.time(), c=contacts(mg.q))
        return _cache["c"]

    def result(msg) -> dict | None:
        if msg._row["raw"] is None:  # do not fetch big mails from IMAP just for a badge
            return None
        return check(msg.email(), msg.from_name, msg.from_addr, msg.html(), known())

    @mg.on_render
    def render(msg):
        res = result(msg)
        if res is None:
            return None
        tone = {"pass": "ok", "fail": "bad", "none": "neutral"}[res["verdict"]]
        text = {"pass": "verified", "fail": "auth failed", "none": "unverified"}[res["verdict"]]
        title = " ".join(f"{k}={v}" for k, v in res["auth"].items()) or "no Authentication-Results header"
        out = {"badges": [{"text": text, "tone": tone, "title": title, "key": f"auth_{res['verdict']}"}],
               "banners": [{"text": w, "tone": "warn"} for w in res["warnings"]]}
        if line := summary(res):
            out["lines"] = [f"Trust: {line}"]
        return out

    @mg.link_filter
    def links(href, text):
        real = link_mismatch(href, text)
        return href, (f"goes to {real}" if real else None)

    @mg.rule_condition("auth")
    def cond_auth(msg, value):
        res = result(msg)
        return bool(res) and res["verdict"] == str(value).lower()

    @mg.rule_condition("suspicious")
    def cond_suspicious(msg, value):
        res = result(msg)
        return bool(res and res["warnings"]) == bool(value)
