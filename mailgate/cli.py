"""Command line interface: `mg <command>`."""
from __future__ import annotations

import argparse
import imaplib
import json
import os
import re
import smtplib
import sys
import time
import urllib.request
from email import policy
from email.parser import BytesParser
from pathlib import Path

from . import __version__, approve, compose, imapsync, smtpsend
from .config import ConfigError, config_path, db_path, example, load
from .store import Store, b36, draft_ref, parse_id

USAGE_NOTE = "Agents can only create drafts. A human approves every send (ntfy, web UI or `mg approve`)."


def out(s: str = "") -> None:
    sys.stdout.write(s + "\n")


def short(s: str | None, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def ts(t: int | None, year: bool = False) -> str:
    return time.strftime("%Y-%m-%d %H:%M" if year else "%m-%d %H:%M", time.localtime(t or 0))


def has_att(r) -> bool:
    return bool(r["atts"]) and r["atts"] != "[]"


def line(r) -> str:
    mark = ("*" if r["unread"] else "") + ("@" if has_att(r) else "")
    return f"{b36(r['id'])}{mark} {ts(r['date'])} {r['acct']}/{r['folder']} {short(r['from_name'], 20)} | {short(r['subject'], 60)}"


def jrow(r) -> dict:
    return {"id": b36(r["id"]), "d": ts(r["date"], True), "a": r["acct"], "f": r["folder"],
            "fr": r["from_name"], "e": r["from_addr"], "s": r["subject"], "u": r["unread"],
            "at": len(json.loads(r["atts"] or "[]"))}


def print_rows(rows, as_json: bool) -> None:
    if as_json:
        out(json.dumps([jrow(r) for r in rows], ensure_ascii=False, separators=(",", ":")))
    else:
        for r in rows:
            out(line(r))


def cap(text: str, n: int) -> str:
    if n and len(text) > n:
        return text[:n].rstrip() + f"\n[...{len(text) - n} more chars, use --full]"
    return text


def _store() -> Store:
    return Store(db_path())


def _msg(store: Store, ref: str):
    m = store.get(parse_id(ref))
    if not m:
        raise ValueError(f"no message {ref}")
    return m


def _filters(a) -> dict:
    return {"acct": a.acct, "folder": a.folder, "sender": getattr(a, "sender", None),
            "since": a.since, "unread": a.unread}


# ---- read side --------------------------------------------------------------

def cmd_ls(a) -> None:
    print_rows(_store().list(limit=a.n, **_filters(a)), a.json)


def cmd_search(a) -> None:
    print_rows(_store().search(" ".join(a.query), limit=a.n, **_filters(a)), a.json)


def cmd_read(a) -> None:
    m = _msg(_store(), a.id)
    atts = json.loads(m["atts"] or "[]")
    from .clean import human_size
    body = m["full"] if a.full else cap(m["body"] or "", a.max)
    if a.json:
        d = {"id": b36(m["id"]), "fr": f"{m['from_name']} <{m['from_addr']}>", "to": m["to_addr"],
             "d": ts(m["date"], True), "s": m["subject"], "b": body}
        if m["cc"]:
            d["cc"] = m["cc"]
        if atts:
            d["at"] = [[n, s] for n, s in atts]
        return out(json.dumps(d, ensure_ascii=False, separators=(",", ":")))
    name = m["from_name"] if m["from_name"] != m["from_addr"] else ""
    out(f"From: {name} <{m['from_addr']}>".replace(":  <", ": <"))
    out(f"To: {m['to_addr']}")
    if m["cc"]:
        out(f"Cc: {m['cc']}")
    if a.full and m["reply_to"]:
        out(f"Reply-To: {m['reply_to']}")
    out(f"Date: {ts(m['date'], True)}")
    out(f"Subject: {m['subject']}")
    if atts:
        out("Att: " + ", ".join(f"{n} ({human_size(s)})" for n, s in atts))
    out()
    out(body)


def cmd_thread(a) -> None:
    rows = _store().thread(parse_id(a.id))
    if not rows:
        raise ValueError(f"no message {a.id}")
    out(f"Thread: {rows[0]['subject']} ({len(rows)} msgs)")
    for r in rows:
        out(f"--- {b36(r['id'])} {ts(r['date'])} {r['from_name']} <{r['from_addr']}>")
        out(r["full"] if a.full else cap(r["body"] or "", a.max))


def cmd_stats(a) -> None:
    store = _store()
    for r in store.stats():
        out(f"{r['acct']}/{r['folder']} {r['n']} msgs, {r['u'] or 0} unread, newest {ts(r['newest'])}")
    out(f"drafts pending: {len(store.pending())}")


def cmd_new(a) -> None:
    rows = _store().watcher_new(a.watcher, acct=a.acct, folder=a.folder, backlog=a.backlog)
    if a.match:
        rx = re.compile(a.match, re.I)
        rows = [r for r in rows if rx.search(f"{r['from_name']} {r['from_addr']} {r['subject']} {r['body']}")]
    print_rows(rows, a.json)


def cmd_att(a) -> None:
    store = _store()
    m = _msg(store, a.id)
    raw = m["raw"]
    if raw is None:
        raw = imapsync.fetch_raw(load().account(m["acct"]), m["folder"], m["uid"])
    msg = BytesParser(policy=policy.default).parsebytes(bytes(raw))
    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    n = 0
    for part in msg.walk():
        name = part.get_filename()
        if part.is_multipart() or not (name or part.get_content_disposition() == "attachment"):
            continue
        safe = re.sub(r"[^\w.\- ()]+", "_", Path(name or "unnamed").name).strip(". ") or "unnamed"
        p = outdir / safe
        i = 1
        while p.exists():
            p = outdir / f"{Path(safe).stem}-{i}{Path(safe).suffix}"
            i += 1
        data = part.get_payload(decode=True) or b""
        p.write_bytes(data)
        out(f"{p} ({len(data)} bytes)")
        n += 1
    if not n:
        out("no attachments")


def cmd_sync(a) -> None:
    cfg = load()
    store = _store()
    names = [a.acct] if a.acct else list(cfg.accounts)
    total = 0
    for n in names:
        total += imapsync.sync_account(store, cfg.account(n), a.folders, cfg.max_raw_bytes, cfg.initial_days, log=out)
    out(f"new: {total}")


# ---- write side ---------------------------------------------------------------

def _body(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text()


def _queued(cfg, store, acct, mime, rcpts, reply_msg=None) -> None:
    q = approve.create_draft(cfg, store, acct, mime, rcpts, reply_msg)
    d = store.draft(q.did)
    ref = draft_ref(q.did)
    if q.state == "sent":
        out(f"{ref} sent automatically (approval mode {cfg.mode_for(acct)}): {q.note}")
    elif q.state == "failed":
        out(f"{ref} automatic send failed: {q.note}")
    elif q.state == "scheduled":
        out(f"{ref} queued, {q.note} by mg daemon (stop it with: mg cancel {ref})")
    else:
        out(f"{ref} queued, waiting for human approval (expires {ts(d['expires'])})")
        if q.note:
            out(f"not sent automatically: {q.note}")
    out(compose.preview(mime, 300))
    if q.warn:
        print("warning: " + q.warn, file=sys.stderr)


def cmd_draft(a) -> None:
    cfg = load()
    acct = cfg.account(a.acct)
    mime, rcpts = compose.build(acct, compose.addr_list(a.to), a.subject, _body(a.body_file),
                                compose.addr_list(a.cc), compose.addr_list(a.bcc), attach=a.attach)
    _queued(cfg, _store(), acct, mime, rcpts)


def cmd_reply(a) -> None:
    cfg = load()
    store = _store()
    m = _msg(store, a.id)
    acct = cfg.account(m["acct"])
    f = compose.reply_fields(acct, m, a.all)
    mime, rcpts = compose.build(acct, f["to"], f["subject"], _body(a.body_file), f["cc"],
                                in_reply_to=f["in_reply_to"], references=f["references"], attach=a.attach)
    _queued(cfg, store, acct, mime, rcpts, reply_msg=m["id"])


def cmd_queue(a) -> None:
    rows = _store().pending()
    for d in rows:
        when = f"auto-send {ts(d['send_at'])}" if d["status"] == "scheduled" else f"expires {ts(d['expires'])}"
        out(f"{draft_ref(d['id'])} {ts(d['created'])} {d['acct']} -> {short(d['to_addr'], 40)} | "
            f"{short(d['subject'], 60)} ({when})")
        if a.full:
            out(compose.preview(bytes(d["mime"])))
            out()
    if not rows:
        out("queue empty")


def cmd_cancel(a) -> None:
    out(approve.reject_draft(_store(), parse_id(a.id, draft=True), via="cli", action="cancelled"))


def cmd_approve(a) -> None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SystemExit("mg approve needs an interactive terminal (a human). Refusing.")
    cfg = load()
    store = _store()
    did = parse_id(a.id, draft=True)
    d = store.draft(did)
    if not d or d["status"] != "pending":
        raise ValueError(f"{a.id}: no pending draft")
    out(compose.preview(bytes(d["mime"])))
    out()
    if input(f"Type the draft id ({draft_ref(did)}) to send it, anything else aborts: ").strip() != draft_ref(did):
        raise SystemExit("aborted, nothing sent")
    out(approve.send_draft(cfg, store, did, via="cli"))


def cmd_log(a) -> None:
    for r in _store().log(a.n):
        out(f"{ts(r['ts'])} {draft_ref(r['draft'])} {r['action']} {r['via']} | {short(r['to_addr'], 40)} | "
            f"{short(r['subject'], 50)}" + (f" | {r['detail']}" if r["detail"] else ""))


def cmd_daemon(a) -> None:
    from . import daemon
    daemon.run(load(), db_path(), web=a.web, use_ntfy=not a.no_ntfy)


# ---- explicit server-side changes (never used by sync) ------------------------------

def cmd_mark(a) -> None:
    from . import imapops
    op = a.op or "read"
    n = imapops.set_flag(_store(), load().accounts, [parse_id(i) for i in a.ids], op)
    out(f"{n} marked {op}")


def cmd_move(a) -> None:
    from . import imapops
    kind = "trash" if a.trash else "archive" if a.archive else None
    if not (a.to or kind):
        raise ValueError("give --to FOLDER, --archive or --trash")
    n, dest = imapops.move(_store(), load().accounts, [parse_id(i) for i in a.ids], target=a.to, kind=kind)
    out(f"{n} moved to {dest}")


def cmd_rules(a) -> None:
    from . import sortrules
    cfg = load()
    store = _store()
    if not cfg.sort_rules:
        return out("no [[rules]] in config")
    if a.action == "test":
        a.n = a.n or 50
        hits = sortrules.plan(cfg, sortrules.recent(store, a.n))
        for r, rule, acts in hits:
            out(f"{b36(r['id'])} {r['acct']}/{r['folder']} {short(r['from_name'], 20)} | {short(r['subject'], 50)}"
                f" -> {rule.name}: {', '.join(acts)}")
        out(f"{len(hits)} of the last {a.n} mails match (dry run, nothing changed)")
        return
    lines = sortrules.apply(cfg, store, sortrules.recent(store, a.n)) if a.n else sortrules.apply_new(cfg, store)
    for ln in lines:
        out(ln)
    out(f"applied to {len(lines)} mails")


def cmd_ui(a) -> None:
    import getpass
    from . import webui
    cfg = load()
    pf = webui.pass_path()
    if a.set_passphrase or (not pf.exists() and sys.stdin.isatty() and sys.stdout.isatty()):
        if not sys.stdin.isatty():
            raise SystemExit("setting the UI passphrase needs an interactive terminal")
        out("The web UI can send mail without the approval queue, so it is protected by a passphrase.")
        if not a.set_passphrase:
            out("Leave it empty to start read-only.")
        pw = getpass.getpass("New UI passphrase: ")
        if pw:
            if getpass.getpass("Repeat: ") != pw:
                raise SystemExit("passphrases differ, nothing changed")
            webui.set_passphrase(pw, pf)
            out(f"stored scrypt hash in {pf}")
        if a.set_passphrase:
            return
    webui.run(cfg, db_path(), a.host, a.port or cfg.ui.port, open_browser=a.open)


# ---- setup ----------------------------------------------------------------------

def cmd_init(a) -> None:
    p = config_path()
    if p.exists() and not a.force:
        raise SystemExit(f"{p} exists (use --force to overwrite)")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(example())
    out(f"wrote {p}, edit it, then run: mg doctor")


def cmd_doctor(a) -> None:
    ok = True

    def check(label: str, fn) -> None:
        nonlocal ok
        try:
            res = fn()
            out(f"ok   {label}" + (f": {res}" if res else ""))
        except Exception as e:
            ok = False
            out(f"FAIL {label}: {type(e).__name__}: {e}")
    cfg = None

    def _cfg():
        nonlocal cfg
        cfg = load()
        return f"{config_path()} ({len(cfg.accounts)} accounts)"
    check("config", _cfg)
    if config_path().exists() and config_path().stat().st_mode & 0o077:
        out(f"warn config is readable by other users, run: chmod 600 {config_path()}")
    check("database", lambda: f"{db_path()} (fts5 {'on' if _store().fts else 'off'})")
    if not cfg:
        raise SystemExit(1)
    for acct in cfg.accounts.values():
        mode = cfg.mode_for(acct)
        if mode == "auto":
            out(f"WARN {acct.name}: approval mode auto, drafts are SENT WITHOUT human approval "
                f"(undo {cfg.undo_seconds}s, max {cfg.max_per_hour}/h)")
        elif mode == "rules":
            r = cfg.rules
            out(f"warn {acct.name}: approval mode rules, matching drafts are sent without approval "
                f"(allow_to {r.allow_to or 'none'}, reply_only {r.reply_only}, undo {cfg.undo_seconds}s)")
        else:
            out(f"ok   {acct.name}: approval mode manual")
    if cfg.undo_seconds and any(cfg.mode_for(a) != "manual" for a in cfg.accounts.values()):
        out("info undo window needs `mg daemon` running, otherwise scheduled drafts are never sent")
    for acct in cfg.accounts.values():
        def imap_check(acct=acct):
            conn = imapsync.connect(acct)
            try:
                have = set(imapsync.list_folders(conn))
            finally:
                conn.logout()
            want = acct.folders + ([acct.sent_folder] if acct.sent_folder else [])
            missing = [f for f in want if imapsync.utf7(f) not in have]
            if missing:
                raise ConfigError(f"folders not on server: {', '.join(missing)}")
            return f"{acct.imap.host}:{acct.imap.port} {acct.imap.security}, {len(have)} folders"

        def smtp_check(acct=acct):
            smtpsend.connect(acct).quit()
            return f"{acct.smtp.host}:{acct.smtp.port} {acct.smtp.security}"
        check(f"{acct.name} imap", imap_check)
        check(f"{acct.name} smtp", smtp_check)
    if cfg.ntfy:
        def ntfy_check():
            req = urllib.request.Request(cfg.ntfy.server + "/v1/health", headers=cfg.ntfy.auth_header())
            with urllib.request.urlopen(req, timeout=15) as r:
                return f"{cfg.ntfy.server} {r.status}"
        check("ntfy", ntfy_check)
    else:
        out("info ntfy not configured (approve via `mg daemon --web` or `mg approve`)")
    if not ok:
        raise SystemExit(1)


# ---- parser ---------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mg", description="Token-efficient mail CLI for AI agents. " + USAGE_NOTE)
    p.add_argument("--version", action="version", version=f"mailgate {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def cmd(name: str, fn, help: str, aliases: tuple = ()) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help, description=help, aliases=list(aliases))
        sp.set_defaults(fn=fn)
        return sp

    def filters(sp, sender: bool = True) -> None:
        sp.add_argument("-n", type=int, default=20, help="max results (default 20)")
        sp.add_argument("--acct")
        sp.add_argument("--folder")
        if sender:
            sp.add_argument("--from", dest="sender", help="sender name/address contains")
        sp.add_argument("--since", help="7d, 24h, 2w or YYYY-MM-DD")
        sp.add_argument("--unread", action="store_true")
        sp.add_argument("--json", action="store_true")

    filters(cmd("ls", cmd_ls, "list cached mail, newest first ('*' unread, '@' attachment)"))
    sp = cmd("search", cmd_search, "full-text search over sender, subject and body")
    sp.add_argument("query", nargs="+")
    filters(sp)
    for name, fn, h in (("read", cmd_read, "show one mail as cleaned text"),
                        ("thread", cmd_thread, "show a whole conversation, oldest first")):
        sp = cmd(name, fn, h)
        sp.add_argument("id")
        sp.add_argument("--full", action="store_true", help="uncut text incl. quotes and footers")
        sp.add_argument("--max", type=int, default=2000 if name == "read" else 1500, help="char cap (0 = none)")
        if name == "read":
            sp.add_argument("--json", action="store_true")
    cmd("stats", cmd_stats, "message counts per account/folder")
    sp = cmd("new", cmd_new, "mails this named watcher has not seen yet (marks them seen)")
    sp.add_argument("watcher")
    sp.add_argument("--match", help="regex on sender/subject/body (case-insensitive)")
    sp.add_argument("--acct")
    sp.add_argument("--folder")
    sp.add_argument("--backlog", action="store_true", help="on first run also print existing mail")
    sp.add_argument("--json", action="store_true")
    sp = cmd("att", cmd_att, "save attachments of a mail")
    sp.add_argument("id")
    sp.add_argument("--out", default=".", help="target directory (default .)")
    sp = cmd("sync", cmd_sync, "fetch new mail via IMAP (read-only)")
    sp.add_argument("--acct")
    sp.add_argument("--folders", nargs="+")

    sp = cmd("draft", cmd_draft, "queue a new mail for human approval")
    sp.add_argument("--acct")
    sp.add_argument("--to", action="append", required=True)
    sp.add_argument("--cc", action="append")
    sp.add_argument("--bcc", action="append")
    sp.add_argument("--subject", required=True)
    sp.add_argument("--body-file", required=True, help="file with the body text, '-' for stdin")
    sp.add_argument("--attach", action="append", help="file to attach (repeatable)")
    sp = cmd("reply", cmd_reply, "queue a reply for human approval")
    sp.add_argument("id")
    sp.add_argument("--body-file", required=True, help="file with the body text, '-' for stdin")
    sp.add_argument("--all", action="store_true", help="reply to all recipients")
    sp.add_argument("--attach", action="append")
    sp = cmd("queue", cmd_queue, "list drafts waiting for approval")
    sp.add_argument("--full", action="store_true", help="show full drafts")
    sp = cmd("cancel", cmd_cancel, "discard a pending draft")
    sp.add_argument("id")
    sp = cmd("approve", cmd_approve, "send a draft (humans only: needs a TTY and retyping the id)")
    sp.add_argument("id")
    sp = cmd("log", cmd_log, "audit log of queued/sent/rejected drafts")
    sp.add_argument("-n", type=int, default=20)
    sp = cmd("daemon", cmd_daemon, "approval listener (ntfy) and optional local web UI")
    sp.add_argument("--web", metavar="HOST:PORT", help="serve approval page, e.g. 127.0.0.1:8765")
    sp.add_argument("--no-ntfy", action="store_true")
    sp = cmd("ui", cmd_ui, "local web mail client for humans (sends without approval, passphrase protected)")
    sp.add_argument("--host", default="127.0.0.1", help="loopback address (default 127.0.0.1)")
    sp.add_argument("--port", type=int, help="default 8766 or [ui] port")
    sp.add_argument("--open", action="store_true", help="open the browser")
    sp.add_argument("--set-passphrase", action="store_true", help="set or change the UI passphrase and exit")
    sp = cmd("mark", cmd_mark, "change flags ON THE SERVER (only when the user asked for it)")
    sp.add_argument("ids", nargs="+")
    g = sp.add_mutually_exclusive_group()
    for flag in ("read", "unread", "flag", "unflag"):
        g.add_argument(f"--{flag}", dest="op", action="store_const", const=flag)
    sp = cmd("move", cmd_move, "move mail ON THE SERVER (only when the user asked for it)")
    sp.add_argument("ids", nargs="+")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--to", metavar="FOLDER")
    g.add_argument("--archive", action="store_true")
    g.add_argument("--trash", action="store_true", help="move to Trash (never deletes permanently)")
    sp = cmd("rules", cmd_rules, "sorting rules: test (dry run) or apply")
    sp.add_argument("action", choices=("test", "apply"))
    sp.add_argument("-n", type=int, default=0, help="last N mails (test default 50; apply default: new mail only)")
    sp = cmd("init", cmd_init, "write an example config")
    sp.add_argument("--force", action="store_true")
    cmd("doctor", cmd_doctor, "check config, approval mode and connectivity (never prints secrets)",
        aliases=("config-check",))
    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `mg` console script."""
    a = parser().parse_args(argv)
    try:
        a.fn(a)
    except BrokenPipeError:
        return 0
    except (ConfigError, ValueError, approve.ApprovalError, imapsync.SyncError, OSError,
            imaplib.IMAP4.error, smtplib.SMTPException) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
