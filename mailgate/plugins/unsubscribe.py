"""Unsubscribe from newsletters (List-Unsubscribe, RFC 2369 / RFC 8058) and a newsletter overview.

- https + List-Unsubscribe-Post: One-Click -> one POST to that https URL, only after you click.
- https without one-click -> the link is shown for you to open yourself (never fetched automatically).
- mailto only -> an unsubscribe mail is queued as a draft and waits for approval like any other.
Plain http targets are never contacted. Optionally adds a [[rules]] entry that moves future
mail from that sender to the Trash, the archive or a folder.

CLI: mg unsub list | mg unsub run SENDER... (interactive terminal only).
"""
from __future__ import annotations

import re
import sys
import time
import urllib.parse
import urllib.request

api_version = 1
TIMEOUT = 20
L = {"unsub": {"en": "Unsubscribe", "de": "Abmelden"}, "view": {"en": "Newsletters", "de": "Newsletter"},
     "rule": {"en": "Unsubscribe + rule for future mail", "de": "Abmelden + Regel für künftige Mails"},
     "confirm": {"en": "Unsubscribe from the selected senders?", "de": "Von den ausgewählten Absendern abmelden?"}}
RULE_CHOICES = [["trash", {"en": "move future mail to Trash", "de": "künftige in den Papierkorb"}],
                ["archive", {"en": "archive future mail", "de": "künftige archivieren"}],
                ["move:Newsletter", {"en": "move future mail to Newsletter", "de": "künftige nach Newsletter"}]]


def targets(header: str) -> dict[str, str]:
    """{'https': url, 'mailto': uri} from a List-Unsubscribe header (plain http is ignored)."""
    out: dict[str, str] = {}
    for uri in re.findall(r"<([^>]+)>", header or ""):
        scheme = uri.split(":", 1)[0].lower()
        if scheme == "https" and "https" not in out:
            out["https"] = uri.strip()
        elif scheme == "mailto" and "mailto" not in out:
            out["mailto"] = uri.strip()
    return out


def method(lu: str, lp: str) -> str:
    t = targets(lu)
    if "https" in t and "one-click" in (lp or "").lower():
        return "one-click"
    return "link" if "https" in t else "mail" if "mailto" in t else "none"


def rule_text(sender: str, action: str) -> str:
    if not re.fullmatch(r"[\w.+=-]+@[\w.-]+", sender) or not re.fullmatch(r"trash|archive|move:[\w ./-]+", action):
        raise ValueError(f"cannot build a rule for {sender!r} / {action!r}")
    rx = "^.*<" + re.escape(sender.lower()) + ">$"  # sender comes from mail: only plain address characters
    return f'\n[[rules]]\nname = "Unsubscribed: {sender}"\nfrom = \'{rx}\'\naction = "{action}"\n'


def setup(mg) -> None:
    def headers(msg) -> dict:
        lu, lp = msg.header("List-Unsubscribe"), msg.header("List-Unsubscribe-Post")
        return {"lu": lu, "lp": lp} if lu else {}

    @mg.on_message_synced
    def remember(msg):
        return headers(msg) or None

    def backfill() -> None:
        """Mail cached before the plugin was enabled: read the headers once from the cache."""
        last = mg.data_get("state", "backfill", 0)
        rows = mg.q("SELECT id FROM msgs WHERE id>? AND raw IS NOT NULL ORDER BY id LIMIT 5000", (last,))
        for (rid,) in rows:
            m = mg.message(rid)
            if m and (h := headers(m)):
                mg.meta_set(m, h)
        if rows:
            mg.data_set("state", "backfill", rows[-1][0])

    def senders() -> list[dict]:
        backfill()
        rows = mg.q("SELECT m.from_addr, MAX(m.from_name) name, COUNT(*) n, MAX(m.date) last, MAX(m.id) latest "
                    "FROM msgs m JOIN plugin_meta p ON p.plugin=? "
                    "AND p.msgid=CASE WHEN m.msgid!='' THEN m.msgid ELSE '#' || m.id END "
                    "GROUP BY m.from_addr ORDER BY n DESC, last DESC", (mg.name,))
        out = []
        for r in rows:
            meta = mg.meta_get(mg.message(r["latest"]))
            done = mg.data_get("done", r["from_addr"])
            out.append({"sender": r["from_addr"], "name": r["name"], "n": r["n"], "last": r["last"],
                        "latest": r["latest"], "method": method(meta.get("lu", ""), meta.get("lp", "")),
                        "done": done["result"] if done else ""})
        return out

    def unsubscribe(sender: str) -> str:
        meta, msg = {}, None
        for m in mg.messages("from_addr=?", (sender.lower(),), limit=50):
            if meta := (mg.meta_get(m) or headers(m)):
                msg = m
                break
        if msg is None:
            raise ValueError(f"no mail with List-Unsubscribe from {sender}")
        t = targets(meta["lu"])
        how = method(meta["lu"], meta.get("lp", ""))
        if how == "one-click":
            req = urllib.request.Request(t["https"], data=b"List-Unsubscribe=One-Click", method="POST",
                                         headers={"Content-Type": "application/x-www-form-urlencoded",
                                                  "User-Agent": "mailgate"})
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # nosec B310: https only (targets())
                    res = f"unsubscribed (one-click, HTTP {r.status})"
            except Exception as e:
                res = f"one-click failed ({e}); open {t['https']}"
        elif how == "link":
            res = f"open this page to unsubscribe: {t['https']}"
        elif how == "mail":
            u = urllib.parse.urlsplit(t["mailto"])
            q = {k.lower(): v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            d = mg.draft([urllib.parse.unquote(u.path)], q.get("subject", "unsubscribe"),
                         q.get("body", "unsubscribe"), acct=msg.acct, signature=False)
            res = f"unsubscribe mail queued as {d}, waiting for approval"
        else:
            raise ValueError(f"{sender}: no usable https or mailto unsubscribe target")
        mg.data_set("done", sender.lower(), {"ts": int(time.time()), "result": res})
        mg.emit("unsubscribed", {"sender": sender, "result": res})
        return res

    def add_rule(sender: str, action: str) -> str:
        from mailgate.config import append_to_config
        append_to_config(rule_text(sender, action))
        return f"rule added ({action})"

    @mg.ui_action("unsubscribe", L["unsub"], when=lambda msg: bool(mg.meta_get(msg).get("lu") or headers(msg)),
                  confirm=L["confirm"], icon="unsub")
    def ui_unsub(msg, choice):
        if not mg.meta_get(msg).get("lu") and (h := headers(msg)):
            mg.meta_set(msg, h)
        return unsubscribe(msg.from_addr)

    @mg.ui_view("newsletters", L["view"], icon="news")
    def view(query):
        return {"items": [{"key": s["sender"], "title": s["name"] or s["sender"], "sub": s["sender"],
                           "meta": f"{s['n']} · {time.strftime('%Y-%m-%d', time.localtime(s['last'] or 0))}"
                                   f" · {s['method']}" + (f" · {s['done']}" if s["done"] else ""),
                           "msg": mg.message(s["latest"]).id} for s in senders()],
                "multi": True, "empty": {"en": "No newsletters found.", "de": "Keine Newsletter gefunden."}}

    @mg.view_action("newsletters", "unsubscribe", L["unsub"], confirm=L["confirm"])
    def view_unsub(keys, choice):
        return "; ".join(f"{k}: {unsubscribe(k)}" for k in keys)

    @mg.view_action("newsletters", "unsubscribe_rule", L["rule"], choices=RULE_CHOICES, confirm=L["confirm"])
    def view_unsub_rule(keys, choice):
        return "; ".join(f"{k}: {unsubscribe(k)}, {add_rule(k, choice or 'trash')}" for k in keys)

    def cli_args(p):
        p.add_argument("action", choices=("list", "run"))
        p.add_argument("senders", nargs="*", help="sender addresses (run)")
        p.add_argument("--rule", choices=("trash", "archive"), help="also add a rule for future mail")

    @mg.command("unsub", "newsletter senders and unsubscribing (run needs an interactive terminal)", cli_args)
    def cli(a):
        if a.action == "list":
            for s in senders():
                print(f"{s['n']:4} {time.strftime('%Y-%m-%d', time.localtime(s['last'] or 0))} {s['method']:9} "
                      f"{s['sender']}" + (f" ({s['done']})" if s["done"] else ""))
            return
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise SystemExit("mg unsub run needs an interactive terminal (a human). Refusing.")
        for sender in a.senders:
            if input(f"Unsubscribe from {sender}? [y/N] ").strip().lower() not in ("y", "j", "yes", "ja"):
                continue
            print(f"{sender}: {unsubscribe(sender)}" + (f", {add_rule(sender, a.rule)}" if a.rule else ""))
