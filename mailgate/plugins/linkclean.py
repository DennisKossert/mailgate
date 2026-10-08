"""Strip tracking parameters (utm_*, fbclid, gclid, mc_eid, ...) from links before you click them.

Settings: [plugins.linkclean] extra = ["ref", "source"]  # more parameter names to strip
Tracking pixels are blocked by the core already (remote images are off by default).
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

api_version = 1
TRACKING = re.compile(
    r"^(utm_\w+|fbclid|gclid|gclsrc|dclid|gbraid|wbraid|msclkid|yclid|twclid|ttclid|li_fat_id|igshid|mc_cid|mc_eid|"
    r"_hsenc|_hsmi|__hssc|__hstc|__hsfp|hsctatracking|mkt_tok|oly_anon_id|oly_enc_id|rb_clickid|s_cid|vero_id|"
    r"vero_conv|wickedid|ml_subscriber|ml_subscriber_hash|sc_cid|trk|trkcampaign|_openstat|ref_src|spm|ncid)$", re.I)


def clean_url(url: str, extra: frozenset = frozenset()) -> str:
    """Remove tracking parameters from an http(s) URL; returns it unchanged if there are none."""
    try:
        u = urlsplit(url)
    except ValueError:
        return url
    if u.scheme.lower() not in ("http", "https") or not u.query:
        return url
    pairs = parse_qsl(u.query, keep_blank_values=True)
    q = [(k, v) for k, v in pairs if not (TRACKING.match(k) or k.lower() in extra)]
    if len(q) == len(pairs):
        return url  # keep the original encoding
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q, doseq=True), u.fragment))


def setup(mg) -> None:
    extra = frozenset(p.lower() for p in mg.settings.get("extra", []))

    @mg.link_filter
    def clean(href, text):
        return clean_url(href, extra), None
