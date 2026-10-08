"""Sorting rules ([[rules]] in the config): match new mail, then move / mark read / flag on the server."""
from __future__ import annotations

from collections import defaultdict
from email import policy
from email.parser import BytesParser

from . import imapops
from .config import Config, SortRule
from .store import Store, b36


def _headers(row):
    raw = row["raw"]
    if raw is None:
        return None
    return BytesParser(policy=policy.default).parsebytes(bytes(raw), headersonly=True)


def matches(rule: SortRule, row) -> bool:
    """All regexes of the rule must match (search, case-insensitive)."""
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
    return True


def plan(cfg: Config, rows) -> list[tuple[object, SortRule, list[str]]]:
    """(row, first rule, actions) per row. Rules are checked in order; a rule with move: ends the search,
    flag-only rules let later rules add a move."""
    out = []
    for row in rows:
        acts: list[str] = []
        first = None
        for rule in cfg.sort_rules:
            if matches(rule, row):
                first = first or rule
                acts += [a for a in rule.actions if a not in acts]
                if any(a.startswith("move:") for a in rule.actions):
                    break
        if first:
            out.append((row, first, acts))
    return out


def apply(cfg: Config, store: Store, rows) -> list[str]:
    """Run the actions on the server (flags first, then moves). Returns log lines."""
    todo = plan(cfg, rows)
    flags: dict[str, list[int]] = defaultdict(list)
    moves: dict[str, list[int]] = defaultdict(list)
    for row, _, acts in todo:
        for a in acts:
            if a == "mark_read" and row["unread"]:
                flags["read"].append(row["id"])
            elif a == "flag":
                flags["flag"].append(row["id"])
            elif a.startswith("move:") and not any(row["id"] in v for v in moves.values()):
                moves[a[5:].strip()].append(row["id"])
    log = [f"{b36(r['id'])} {r['acct']}/{r['folder']} | {r['subject']} -> {rule.name}: {', '.join(acts)}"
           for r, rule, acts in todo]
    for op, ids in flags.items():
        imapops.set_flag(store, cfg.accounts, ids, op)
    for folder, ids in moves.items():
        imapops.move(store, cfg.accounts, ids, target=folder)
    return log


def apply_new(cfg: Config, store: Store) -> list[str]:
    """Apply rules to mail cached since the last call. The first call only sets the starting point."""
    top = store.one("SELECT COALESCE(MAX(id), 0) FROM msgs")[0]
    last = store.kv_get("rules_last_id")
    store.kv_set("rules_last_id", str(max(top, int(last or 0))))
    if last is None or not cfg.sort_rules:
        return []
    return apply(cfg, store, store.q("SELECT * FROM msgs WHERE id>? ORDER BY id", (int(last),)))


def recent(store: Store, n: int):
    return store.q("SELECT * FROM msgs ORDER BY date DESC, id DESC LIMIT ?", (n,))
