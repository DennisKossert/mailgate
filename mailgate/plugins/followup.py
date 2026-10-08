"""Follow-up reminders ("Wiedervorlage") and snooze.

    mg remind ID --in 5d [--if-no-reply]   remind me in 5 days (only if nobody answered)
    mg remind --list | --done KEY | --cancel KEY
    mg snooze ID --until 2026-10-12        hide from the inbox views until then

When a reminder is due (checked every minute by mg ui / mg daemon --sync) you get an ntfy
notification (if configured) and a badge in the UI. Reminders are stored by Message-ID, so
they survive a resync and `mg export`.
Settings: [plugins.followup] default = "3d"
"""
from __future__ import annotations

import re
import time
from datetime import datetime

api_version = 1
L = {"remind": {"en": "Follow up", "de": "Wiedervorlage"}, "snooze": {"en": "Snooze", "de": "Zurückstellen"},
     "view": {"en": "Follow-ups", "de": "Wiedervorlagen"}, "done": {"en": "Done", "de": "Erledigt"},
     "cancel": {"en": "Remove", "de": "Entfernen"}}
REMIND_CHOICES = [["1d", {"en": "in 1 day", "de": "in 1 Tag"}], ["3d", {"en": "in 3 days", "de": "in 3 Tagen"}],
                  ["1w", {"en": "in 1 week", "de": "in 1 Woche"}],
                  ["3d!", {"en": "in 3 days if no reply", "de": "in 3 Tagen, falls keine Antwort"}],
                  ["1w!", {"en": "in 1 week if no reply", "de": "in 1 Woche, falls keine Antwort"}]]
SNOOZE_CHOICES = [["tomorrow", {"en": "until tomorrow 9:00", "de": "bis morgen 9 Uhr"}],
                  ["2d", {"en": "for 2 days", "de": "für 2 Tage"}], ["1w", {"en": "for 1 week", "de": "für 1 Woche"}]]


def parse_when(s: str, now: float | None = None) -> int:
    """'5d', '12h', '2w', '30m', 'tomorrow', 'YYYY-MM-DD' (09:00) or 'YYYY-MM-DD HH:MM' -> epoch seconds."""
    now = now or time.time()
    s = s.strip().lower()
    if s == "tomorrow":
        d = datetime.fromtimestamp(now + 86400)
        return int(d.replace(hour=9, minute=0, second=0, microsecond=0).timestamp())
    if m := re.fullmatch(r"(\d+)\s*([mhdw])", s):
        return int(now + int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)])
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dt%H:%M", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s, fmt)
            return int((d.replace(hour=9) if fmt == "%Y-%m-%d" else d).timestamp())
        except ValueError:
            pass
    raise ValueError(f"bad time {s!r} (use 5d, 12h, 2w, tomorrow or YYYY-MM-DD [HH:MM])")


def setup(mg) -> None:
    mine = {a["email"].lower() for a in mg.accounts}

    def add(msg, due: int, kind: str, if_no_reply: bool = False) -> str:
        if kind == "snooze":
            for k, r in mg.data_list("r"):
                if r["kind"] == "snooze" and r["msgid"] == msg.msgid and r["status"] == "waiting":
                    mg.data_del("r", k)
        key = str(time.time_ns())
        mg.data_set("r", key, {"kind": kind, "msgid": msg.msgid, "thread": msg.thread or msg.msgid,
                               "acct": msg.acct, "subject": msg.subject, "from": msg.from_addr,
                               "created": int(time.time()), "due": due, "if_no_reply": if_no_reply,
                               "status": "waiting", "msg": msg.id})
        return key

    def replied(r) -> bool:
        """Did someone other than me write in this conversation after the reminder was set?"""
        marks = ",".join("?" * len(mine)) or "''"
        return bool(mg.q(f"SELECT 1 FROM msgs WHERE (thread=? OR irt=? OR refs LIKE ?) AND date>? "
                         f"AND from_addr NOT IN ({marks}) LIMIT 1",
                         [r["thread"], r["msgid"], f"%{r['msgid']}%", r["created"], *mine]))

    def set_status(key: str, status: str) -> str:
        r = mg.data_get("r", key)
        if not r:
            raise ValueError(f"no reminder {key}")
        mg.data_set("r", key, r | {"status": status})
        return f"{key} {status}"

    @mg.every(1)
    def check():
        now = time.time()
        for key, r in mg.data_list("r"):
            if r["status"] != "waiting" or r["due"] > now:
                continue
            if r["kind"] == "snooze":
                set_status(key, "done")
            elif r["if_no_reply"] and replied(r):
                set_status(key, "answered")
            else:
                set_status(key, "due")
                mg.notify(f"Follow up: {r['subject']} ({r['from']})" + (", no reply yet" if r["if_no_reply"] else ""),
                          title="mailgate: Wiedervorlage / follow-up")

    @mg.list_filter
    def hide_snoozed(items, view):
        if view.get("kind") == "search" or (view.get("folder") not in (None, "INBOX")):
            return items
        now = time.time()
        hidden = {r["msgid"] for _, r in mg.data_list("r")
                  if r["kind"] == "snooze" and r["status"] == "waiting" and r["due"] > now}
        return [i for i in items if i.get("mi") not in hidden] if hidden else items

    @mg.ui_action("remind", L["remind"], choices=REMIND_CHOICES, icon="clock")
    def ui_remind(msg, choice):
        choice = choice or mg.settings.get("default", "3d")
        due = parse_when(choice.rstrip("!"))
        add(msg, due, "remind", choice.endswith("!"))
        return f"reminder set for {time.strftime('%Y-%m-%d %H:%M', time.localtime(due))}"

    @mg.ui_action("snooze", L["snooze"], choices=SNOOZE_CHOICES, icon="snooze")
    def ui_snooze(msg, choice):
        due = parse_when(choice or "tomorrow")
        add(msg, due, "snooze")
        return f"snoozed until {time.strftime('%Y-%m-%d %H:%M', time.localtime(due))}"

    def badge() -> int:
        return sum(1 for _, r in mg.data_list("r") if r["status"] == "due")

    @mg.ui_view("followups", L["view"], badge=badge, icon="clock")
    def view(query):
        items = []
        for key, r in sorted(mg.data_list("r"), key=lambda kr: kr[1]["due"]):
            if r["status"] not in ("waiting", "due"):
                continue
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["due"]))
            label = {"remind": "⏰", "snooze": "💤"}[r["kind"]]
            items.append({"key": key, "title": r["subject"] or "-", "sub": r["from"],
                          "meta": f"{label} {when}" + (" · due" if r["status"] == "due" else "")
                                  + (" · if no reply" if r["if_no_reply"] else ""),
                          "msg": r.get("msg"), "hot": r["status"] == "due"})
        return {"items": items, "multi": True,
                "empty": {"en": "No follow-ups.", "de": "Keine Wiedervorlagen."}}

    @mg.view_action("followups", "done", L["done"])
    def view_done(keys, choice):
        return ", ".join(set_status(k, "done") for k in keys)

    @mg.view_action("followups", "cancel", L["cancel"])
    def view_cancel(keys, choice):
        return ", ".join(set_status(k, "cancelled") for k in keys)

    @mg.tui_command("remind", "remind [3d|1w|DATE] [!]: follow up later (! = only if nobody replied)")
    def tui_remind(tui, args, msgs):
        words = args.split()
        when = next((w for w in words if w != "!"), mg.settings.get("default", "3d"))
        noreply = "!" in words or when.endswith("!")
        due = parse_when(when.rstrip("!"))
        for m in msgs:
            add(m, due, "remind", noreply)
        return f"{len(msgs)} reminder(s) for {time.strftime('%Y-%m-%d %H:%M', time.localtime(due))}"

    @mg.tui_command("snooze", "snooze [tomorrow|2d|DATE]: hide from the inbox until then")
    def tui_snooze(tui, args, msgs):
        due = parse_when(args.strip() or "tomorrow")
        for m in msgs:
            add(m, due, "snooze")
        return f"{len(msgs)} snoozed until {time.strftime('%Y-%m-%d %H:%M', time.localtime(due))}"

    @mg.tui_statusline
    def tui_due(tui):
        n = badge()
        return f"{n} follow-up{'s' if n != 1 else ''} due" if n else ""

    def remind_args(p):
        p.add_argument("id", nargs="?")
        p.add_argument("--in", dest="when", help="5d, 12h, 2w, tomorrow or YYYY-MM-DD [HH:MM]")
        p.add_argument("--at", dest="when")
        p.add_argument("--if-no-reply", action="store_true", help="only if nobody else wrote in the thread")
        p.add_argument("--list", action="store_true")
        p.add_argument("--done", metavar="KEY")
        p.add_argument("--cancel", metavar="KEY")

    @mg.command("remind", "follow-up reminders (Wiedervorlage)", remind_args)
    def cli_remind(a):
        if a.done or a.cancel:
            print(set_status(a.done or a.cancel, "done" if a.done else "cancelled"))
        elif a.list or not a.id:
            for key, r in mg.data_list("r"):
                if r["status"] in ("waiting", "due"):
                    print(f"{key} {r['kind']:6} {time.strftime('%m-%d %H:%M', time.localtime(r['due']))} "
                          f"{r['status']:7} {r['from']} | {r['subject']}")
        else:
            msg = mg.message(a.id)
            if not msg:
                raise ValueError(f"no message {a.id}")
            due = parse_when(a.when or mg.settings.get("default", "3d"))
            print(f"reminder {add(msg, due, 'remind', a.if_no_reply)} "
                  f"for {time.strftime('%Y-%m-%d %H:%M', time.localtime(due))}")

    def snooze_args(p):
        p.add_argument("id")
        p.add_argument("--until", "--in", dest="when", default="tomorrow")

    @mg.command("snooze", "hide a mail from the inbox views until a time", snooze_args)
    def cli_snooze(a):
        msg = mg.message(a.id)
        if not msg:
            raise ValueError(f"no message {a.id}")
        due = parse_when(a.when)
        add(msg, due, "snooze")
        print(f"snoozed until {time.strftime('%Y-%m-%d %H:%M', time.localtime(due))}")
