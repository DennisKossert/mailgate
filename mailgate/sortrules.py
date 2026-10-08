"""Sorting rules ([[rules]] in the config): match new mail, then act on it.

Core actions: move:<folder>, archive, trash, mark_read, flag (on the IMAP server) and
run:<command> (local hook). Plugins add conditions (extra rule keys) and actions.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections import defaultdict
from email import policy
from email.parser import BytesParser
from typing import Callable

from . import imapops
from .config import CORE_ACTIONS, Config, SortRule
from .store import Store, b36

RUN_TIMEOUT = 60
RUN_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "SYSTEMROOT")


def _headers(row):
    raw = row["raw"]
    if raw is None:
        return None
    return BytesParser(policy=policy.default).parsebytes(bytes(raw), headersonly=True)


def matches(rule: SortRule, row, reg=None) -> bool:
    """All conditions must match: core regexes (case-insensitive search) and plugin conditions."""
    if rule.account and row["acct"] != rule.account:
        return False
    if rule.folder and row["folder"] != rule.folder:
        return False
    hdrs = _headers(row) if (rule.headers or "list_id" in rule.match) else None
    fields = {"from": f"{row['from_name']} <{row['from_addr']}>", "subject": row["subject"] or "",
              "to": f"{row['to_addr'] or ''}, {row['cc'] or ''}",
              "list_id": str(hdrs.get("List-Id", "")) if hdrs is not None else ""}
    if not all(rx.search(fields[k]) for k, rx in rule.match.items()):
        return False
    for name, rx in rule.headers.items():
        if hdrs is None or not any(rx.search(str(v)) for v in hdrs.get_all(name, [])):
            return False
    for key, value in rule.extra.items():
        if reg is not None and key in reg.conditions:
            api, fn = reg.conditions[key]
            from .plugin import Msg
            if not reg.call(api, fn, Msg(api, row), value, default=False):
                return False
        elif not _is_option(key, rule, reg):  # unknown condition (plugin not enabled): never match
            return False
    return True


def _is_option(key: str, rule: SortRule, reg) -> bool:
    """Extra key read by one of the rule's plugin actions (e.g. save_types)."""
    if reg is None:
        return False
    return any(key in reg.actions.get(a.split(":", 1)[0], (None, None, set()))[2] for a in rule.actions)


def problems(cfg: Config, reg=None) -> list[str]:
    """Rule keys/actions nothing understands (plugin missing?), for mg rules test and mg doctor."""
    out = []
    for r in cfg.sort_rules:
        for k in r.extra:
            if not (reg is not None and k in reg.conditions) and not _is_option(k, r, reg):
                out.append(f"rule '{r.name}': unknown key {k!r} (is its plugin enabled?), rule never matches")
        for a in r.actions:
            kind = a.split(":", 1)[0]
            if kind not in CORE_ACTIONS and not (reg is not None and kind in reg.actions):
                out.append(f"rule '{r.name}': unknown action {kind!r} (is its plugin enabled?)")
    return out


def plan(cfg: Config, rows, reg=None) -> list[tuple[object, SortRule, list[str]]]:
    """(row, first rule, actions) per row. Rules are checked in order; a rule that moves the mail
    (move:, archive, trash) ends the search, other rules let later rules add actions."""
    out = []
    for row in rows:
        acts: list[str] = []
        first = None
        for rule in cfg.sort_rules:
            if matches(rule, row, reg):
                first = first or rule
                acts += [a for a in rule.actions if a not in acts]
                if any(_is_move(a) for a in rule.actions):
                    break
        if first:
            out.append((row, first, acts))
    return out


def _is_move(a: str) -> bool:
    return a.startswith("move:") or a in ("archive", "trash")


def run_hook(row, rule: SortRule, command: str) -> int:
    """Run a local command for a matching mail. No shell: the command is split with shlex, mail data
    goes only into environment variables and JSON on stdin, never into the command line."""
    meta = {"id": b36(row["id"]), "account": row["acct"], "folder": row["folder"], "from": row["from_addr"],
            "from_name": row["from_name"], "to": row["to_addr"], "subject": row["subject"], "date": row["date"],
            "message_id": row["msgid"], "rule": rule.name,
            "attachments": [n for n, _ in json.loads(row["atts"] or "[]")], "body": row["body"]}
    env = {k: os.environ[k] for k in RUN_ENV if k in os.environ}  # no MAILGATE_*, no password_env values
    env.update({f"MG_{k.upper()}": " ".join(str(v).split())[:500] for k, v in meta.items()
                if k not in ("attachments", "body")})
    argv = shlex.split(os.path.expanduser(command))  # from the config only; mail data never goes into argv
    r = subprocess.run(argv, input=json.dumps(meta, ensure_ascii=False), text=True, env=env,  # nosec B603
                       timeout=RUN_TIMEOUT, capture_output=True, shell=False)
    return r.returncode


def apply(cfg: Config, store: Store, rows, log: Callable[[str], None] = lambda s: None, reg=None) -> list[str]:
    """Run the actions (local ones and flags first, moves last). Returns one line per matching mail."""
    todo = plan(cfg, rows, reg)
    flags: dict[str, list[int]] = defaultdict(list)
    moves: dict[tuple[str, str], list[int]] = defaultdict(list)
    lines = []
    for row, rule, acts in todo:
        notes = []
        for a in acts:
            kind, _, arg = a.partition(":")
            try:
                if a == "mark_read" and row["unread"]:
                    flags["read"].append(row["id"])
                elif a == "flag":
                    flags["flag"].append(row["id"])
                elif kind == "run":
                    notes.append(f"run exit {run_hook(row, rule, arg)}")
                elif _is_move(a) and not any(row["id"] in v for v in moves.values()):
                    moves[("move", arg.strip()) if kind == "move" else (a, "")].append(row["id"])
                elif reg is not None and kind in reg.actions:
                    api, fn, _ = reg.actions[kind]
                    from .plugin import Msg
                    res = reg.call(api, fn, Msg(api, row), arg, rule)
                    if res:
                        notes.append(str(res))
                elif kind not in CORE_ACTIONS:
                    notes.append(f"{kind}: no plugin")
            except Exception as e:
                log(f"{b36(row['id'])} {a}: {type(e).__name__}: {e}")
                notes.append(f"{kind} failed")
        lines.append(f"{b36(row['id'])} {row['acct']}/{row['folder']} | {row['subject']} -> {rule.name}: "
                     f"{', '.join(acts)}" + (f" ({'; '.join(notes)})" if notes else ""))
    for op, ids in flags.items():
        imapops.set_flag(store, cfg.accounts, ids, op)
    for (kind, folder), ids in moves.items():
        imapops.move(store, cfg.accounts, ids, target=folder or None, kind=None if folder else kind)
    return lines


def claim_new(store: Store, key: str) -> list:
    """Rows cached since the last call for this pointer (atomic across processes). The first call only
    sets the starting point, so enabling something never acts on old mail."""
    store.db.execute("BEGIN IMMEDIATE")
    try:
        top = store.one("SELECT COALESCE(MAX(id), 0) FROM msgs")[0]
        last = store.kv_get(key)
        store.kv_set(key, str(max(top, int(last or 0))))
    finally:
        store.db.execute("COMMIT")
    if last is None:
        return []
    return store.q("SELECT * FROM msgs WHERE id>? AND id<=? ORDER BY id", (int(last), top))


def apply_new(cfg: Config, store: Store, log: Callable[[str], None] = lambda s: None, reg=None) -> list[str]:
    """Apply rules to mail cached since the last call (pointer rules_last_id)."""
    rows = claim_new(store, "rules_last_id")
    return apply(cfg, store, rows, log, reg) if rows and cfg.sort_rules else []


def recent(store: Store, n: int):
    return store.q("SELECT * FROM msgs ORDER BY date DESC, id DESC LIMIT ?", (n,))
