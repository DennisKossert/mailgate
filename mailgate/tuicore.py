"""Logic of `mg tui` without curses: settings, key engine, command parser, commands, state.

The curses frontend (tui.py) only draws and reads keys; everything here is testable.
Sending is only possible through Controller.send(), which needs a draft id typed at the
real keyboard (never from a macro, a keymap run from on_start, or plugin code).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
import tomllib
import unicodedata
from dataclasses import dataclass, field
from email.utils import formataddr
from pathlib import Path
from typing import Callable

from . import approve, compose, imapops
from .clean import html_body, term_safe
from .config import Config, ConfigError, config_path
from .store import Store, b36, draft_ref, parse_id

LIST_EXTRA = ", m.flags, m.msgid, m.thread, m.to_addr"
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
DEFAULT_KEYS: dict[str, dict[str, str]] = {
    "normal": {"j": "down", "k": "up", "<Down>": "down", "<Up>": "up", "gg": "top", "G": "bottom",
               "<C-d>": "pagedown", "<C-u>": "pageup", "<PageDown>": "pagedown", "<PageUp>": "pageup",
               "<Enter>": "open", "l": "open", "<Right>": "open", "/": "prompt search", "n": "down", "N": "up",
               ":": "prompt command", "V": "visual", "v": "visual", "r": "reply", "a": "reply all",
               "f": "forward", "c": "compose", "e": "archive", "#": "trash", "dd": "trash",
               "u": "toggle unread", "s": "toggle flag", "M": "prompt command move ", "F": "folders",
               "h": "folders", "<Left>": "folders", "R": "sync", "t": "set threaded!", "?": "help",
               "Q": "quit", "ZZ": "quit", "gl": "links", "D": "drafts", "<Tab>": "focus"},
    "reader": {"j": "scroll 1", "k": "scroll -1", "<Down>": "scroll 1", "<Up>": "scroll -1",
               "<Space>": "scroll page", "<C-d>": "scroll half", "<C-u>": "scroll -half", "gg": "scroll top",
               "G": "scroll bottom", "q": "close", "<Esc>": "close", "h": "close", "<Left>": "close",
               "J": "next", "K": "prev", "/": "prompt find", "n": "findnext", "N": "findprev",
               ":": "prompt command", "r": "reply", "a": "reply all", "f": "forward", "e": "archive",
               "#": "trash", "u": "toggle unread", "s": "toggle flag", "T": "text", "H": "html",
               "gl": "links", "<Tab>": "focus", "?": "help", "Q": "quit"},
    "visual": {"j": "down", "k": "up", "<Down>": "down", "<Up>": "up", "G": "bottom", "gg": "top",
               "<Esc>": "visual", "V": "visual", "v": "visual", "e": "archive", "#": "trash", "d": "trash",
               "u": "mark unread", "U": "mark read", "s": "mark flag", "M": "prompt command move ",
               ":": "prompt command"},
    "folders": {"j": "down", "k": "up", "<Down>": "down", "<Up>": "up", "<Enter>": "open", "l": "open",
                "<Right>": "open", "q": "focus", "<Esc>": "focus", "<Tab>": "focus", "F": "focus",
                "gg": "top", "G": "bottom", ":": "prompt command", "Q": "quit"},
}
DEFAULT_COLORS = {"normal": "default,default", "unread": "default,default,bold", "selected": "black,cyan",
                  "visual": "black,yellow", "status": "black,white", "statusmode": "white,blue,bold",
                  "header": "cyan,default", "quote": "green,default", "link": "blue,default,underline",
                  "badge_ok": "green,default,bold", "badge_bad": "red,default,bold",
                  "badge_neutral": "yellow,default", "warn": "red,default", "folder": "default,default",
                  "folder_current": "black,cyan", "search": "black,yellow", "error": "red,default,bold",
                  "tree": "blue,default", "dim": "default,default,dim"}


@dataclass
class Settings:
    """~/.config/mailgate/tui.toml"""
    keys: dict[str, dict[str, str]] = field(default_factory=lambda: {m: dict(k) for m, k in DEFAULT_KEYS.items()})
    colors: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COLORS))
    layout: str = "split"  # split (list above reader) | vsplit (side by side) | full (one pane)
    sidebar: bool = True  # folder list on the left
    sidebar_width: int = 24
    split_ratio: float = 0.4  # share of the list pane
    index_format: str = "{flags:3} {date:>12}  {from:22.22}  {tree}{subject}"
    date_format: str = "%d.%m.%y"
    date_today: str = "%H:%M"
    sort: str = "-date"  # -date | date | from | subject | unread
    threaded: bool = False
    text: str = "clean"  # clean (like mg read) | full
    mark_read: bool = True
    html_viewer: list[str] = field(default_factory=list)  # e.g. ["w3m", "-dump", "-T", "text/html", "{file}"]
    opener: list[str] = field(default_factory=list)  # default: $BROWSER, else xdg-open / open
    editor: list[str] = field(default_factory=list)  # default: $VISUAL / $EDITOR / vi
    quote_intro: str = "On {date}, {from} wrote:"
    on_start: list[str] = field(default_factory=list)
    page: int = 300


def tui_path() -> Path:
    return config_path().parent / "tui.toml"


def load_settings(path: Path | None = None) -> Settings:
    path = path or tui_path()
    st = Settings()
    if not path.exists():
        return st
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    for mode, keys in (data.pop("keys", None) or {}).items():
        if mode not in DEFAULT_KEYS:
            raise ConfigError(f"{path}: unknown key mode {mode!r} (normal, reader, visual, folders)")
        for k, cmd in keys.items():
            if cmd in ("", None):
                st.keys[mode].pop(k, None)  # "x" = "" removes a default binding
            else:
                st.keys[mode][k] = str(cmd)
    st.colors.update({k: str(v) for k, v in (data.pop("colors", None) or {}).items()})
    for k, v in data.items():
        if not hasattr(st, k):
            raise ConfigError(f"{path}: unknown setting {k!r}")
        cur = getattr(st, k)
        if isinstance(cur, list) and isinstance(v, str):
            v = shlex.split(v) if k in ("html_viewer", "opener", "editor") else [v]
        if type(cur) is not type(v) and not (isinstance(cur, float) and isinstance(v, int)):
            raise ConfigError(f"{path}: {k} must be {type(cur).__name__}")
        setattr(st, k, v)
    if st.layout not in ("split", "vsplit", "full"):
        raise ConfigError(f"{path}: layout must be split, vsplit or full")
    return st


# ---- command lines ------------------------------------------------------------------------------

def split_pipeline(line: str) -> list[list[str]]:
    """':move Archiv | mark read' -> [['move', 'Archiv'], ['mark', 'read']]. Quotes protect '|'."""
    lex = shlex.shlex(line.strip().lstrip(":"), posix=True, punctuation_chars="|")
    lex.whitespace_split = True
    lex.commenters = ""
    out: list[list[str]] = [[]]
    try:
        for tok in lex:
            if tok and set(tok) == {"|"}:
                out.extend([] for _ in tok)
            else:
                out[-1].append(tok)
    except ValueError as e:
        raise ValueError(f"cannot parse: {e}") from None
    if any(not part for part in out) and line.strip(" :"):
        raise ValueError("empty command in pipeline")
    return [p for p in out if p]


# ---- keys ---------------------------------------------------------------------------------------------

@dataclass
class KeyAction:
    command: str = ""
    count: int = 1
    special: str = ""  # mark | jump | record | play
    arg: str = ""


class KeyEngine:
    """Vim-like key resolution: counts (5j), multi-key sequences (gg, dd), marks (ma, 'a),
    macros (qa ... q, @a, @@). feed(key, mode) returns a KeyAction, or None while waiting."""

    SPECIAL = {"m": "mark", "'": "jump", "q": "record", "@": "play"}

    def __init__(self, keys: dict[str, dict[str, str]]):
        self.keys = keys
        self.pending = ""
        self.count = ""
        self.special = ""
        self.recording: str | None = None

    def reset(self) -> None:
        self.pending = self.count = self.special = ""

    def feed(self, key: str, mode: str) -> KeyAction | None:
        if self.special:
            sp, self.special = self.special, ""
            n = int(self.count or 1)
            self.count = ""
            if key == "<Esc>":
                return None
            if sp == "play" and key == "@":
                key = "@"  # @@ = last macro
            if sp != "play" and not re.fullmatch(r"[a-zA-Z0-9]", key):
                return None
            return KeyAction(special=sp, arg=key, count=n)
        table = self.keys.get(mode, {})
        if not self.pending and key.isdigit() and (self.count or key != "0") and key not in table:
            self.count += key
            return None
        if not self.pending and mode in ("normal", "reader") and key in self.SPECIAL and key not in table:
            if key == "q" and self.recording is not None:
                return KeyAction(special="stop")
            if key == "q" and mode != "normal":
                pass
            else:
                self.special = self.SPECIAL[key]
                return None
        seq = self.pending + key
        longer = any(k != seq and k.startswith(seq) for k in table)
        if seq in table and not longer:
            n = int(self.count or 1)
            self.reset()
            return KeyAction(command=table[seq], count=n)
        if longer:
            self.pending = seq
            return None
        if self.pending and self.pending in table:  # e.g. "g" mapped and "gx" unknown: run "g", retry x
            cmd, n = table[self.pending], int(self.count or 1)
            self.reset()
            return KeyAction(command=cmd, count=n)
        self.reset()
        return None


# ---- text helpers ---------------------------------------------------------------------------------

def width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 0 if unicodedata.combining(c) else 1 for c in s)


def fit(s: str, n: int) -> str:
    """Cut to n terminal cells, pad with spaces."""
    out, w = [], 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in "WF" else 0 if unicodedata.combining(c) else 1
        if w + cw > n:
            break
        out.append(c)
        w += cw
    return "".join(out) + " " * (n - w)


def wrap(text: str, n: int) -> list[str]:
    import textwrap
    lines = []
    for ln in text.split("\n"):
        ln = ln.replace("\t", "    ")
        lines += textwrap.wrap(ln, n, break_long_words=True, break_on_hyphens=False,
                               drop_whitespace=False) or [""] if ln.strip() else [""]
    return lines


class _Fmt(dict):
    def __missing__(self, key):
        return ""


def fmt_date(t: int | None, st: Settings) -> str:
    lt = time.localtime(t or 0)
    today = time.localtime()
    same = lt.tm_year == today.tm_year and lt.tm_yday == today.tm_yday
    return time.strftime(st.date_today if same else st.date_format, lt)


# ---- controller ------------------------------------------------------------------------------------

@dataclass
class Item:
    row: object
    tree: str = ""
    dup: int = 0

    @property
    def id(self) -> int:
        return self.row["id"]


class Frontend:
    """What the controller needs from the screen. tui.Curses implements it; tests use a fake."""

    tty = False  # True when ask() reads from a real terminal

    def ask(self, prompt: str, prefill: str = "", complete: Callable | None = None) -> str:
        """Read a line typed at the real keyboard ('' on Esc)."""
        raise NotImplementedError

    def edit(self, text: str) -> str | None:  # run the editor on text, None if unchanged/aborted
        raise NotImplementedError

    def run_external(self, argv: list[str]) -> None:
        subprocess.run(argv, check=False)  # nosec B603: argv list from tui.toml, no shell

    def redraw(self) -> None:
        pass


class CommandError(Exception):
    """A command failed; shown in the status line."""


class Controller:
    def __init__(self, cfg: Config, db: Path, settings: Settings | None = None, reg=None,
                 frontend: Frontend | None = None):
        self.cfg, self.db = cfg, db
        self.st = settings or Settings()
        self.store = Store(db)
        self.reg = reg
        self.ui = frontend or Frontend()
        self.mode = "normal"
        self.view: dict = {"kind": "unified"}
        self.items: list[Item] = []
        self.more = ""  # keyset cursor for the next page
        self.cur = 0
        self.top = 0
        self.anchor: int | None = None  # visual mode start
        self.marks: dict[str, tuple[dict, int]] = {}
        self.macros: dict[str, list[str]] = {}
        self.last_macro = ""
        self.recorded: list[str] = []
        self.source = "user"  # user | replay | start | plugin: where the running command came from
        self.msg = None  # open message row
        self.lines: list[tuple[str, str]] = []  # (style, text) of the reader
        self.scroll = 0
        self.text_mode = self.st.text
        self.links: list[tuple[str, list[str]]] = []
        self.find = ""
        self.status = ""
        self.error = False
        self.folders: list[tuple[str, dict]] = []
        self.fcur = 0
        self.quit = False
        self.syncing = False
        self.engine = KeyEngine(self._keymap())
        self.commands = self._builtin_commands()
        if self.reg is not None:
            for name in self.reg.tui_commands:
                self.commands.setdefault(name, (self._plugin_cmd(name), self.reg.tui_commands[name][2]))

    # ---- setup ----------------------------------------------------------------------------
    def _keymap(self) -> dict[str, dict[str, str]]:
        keys = {m: dict(k) for m, k in self.st.keys.items()}
        if self.reg is not None:  # plugin defaults, user's tui.toml wins
            for mode, key, cmd in self.reg.tui_keys:
                if mode in keys and key not in self.st.keys.get(mode, {}):
                    keys[mode][key] = cmd
        return keys

    def start(self) -> None:
        self.load_folders()
        self.load(reset=True)
        for line in self.st.on_start:
            self.execute(line, source="start")

    # ---- data ------------------------------------------------------------------------------
    def load_folders(self) -> None:
        counts = {(r["acct"], r["folder"]): r["u"] or 0 for r in self.store.stats()}
        unified = sum(u for (a, f), u in counts.items() if f == "INBOX")
        out = [(f"All inboxes ({unified})" if unified else "All inboxes", {"kind": "unified"}),
               (f"Drafts to approve ({len(self.store.pending())})", {"kind": "drafts"})]
        for a in self.cfg.accounts.values():
            names = list(dict.fromkeys(a.folders + sorted(f for (ac, f) in counts if ac == a.name)))
            out.append((f"{a.name}", {"kind": "header"}))
            for f in names:
                u = counts.get((a.name, f), 0)
                out.append((f"  {f}" + (f" ({u})" if u else ""), {"kind": "folder", "acct": a.name, "folder": f}))
        self.folders = out

    def view_title(self) -> str:
        v = self.view
        return {"unified": "All inboxes", "drafts": "Drafts to approve",
                "search": f"search: {v.get('q')}"}.get(v["kind"], f"{v.get('acct')}/{v.get('folder')}")

    def load(self, reset: bool = False) -> None:
        """(Re)load the list for the current view; keeps the cursor on the same message if possible."""
        keep = self.items[self.cur].id if self.items and 0 <= self.cur < len(self.items) else None
        if self.view["kind"] == "drafts":
            self.items = [Item(d) for d in self.store.pending()]
            self.more = ""
        else:
            f = {"acct": self.view.get("acct"), "folder": self.view.get("folder")}
            if self.view["kind"] == "unified":
                f["folder"] = "INBOX"
            n = self.st.page
            if self.view["kind"] == "search":
                rows = self.store.search(self.view["q"], limit=n, extra=LIST_EXTRA, **f)
            else:
                rows = self.store.list(limit=n, extra=LIST_EXTRA, **f)
            self.more = f"{rows[-1]['date']}.{rows[-1]['id']}" if len(rows) == n else ""
            self.items = self._arrange([Item(r) for r in rows])
        self.cur = next((i for i, it in enumerate(self.items) if it.id == keep), 0) if not reset else 0
        self.cur = min(self.cur, max(0, len(self.items) - 1))
        if reset:
            self.top = 0

    def load_more(self) -> None:
        if not self.more or self.view["kind"] == "drafts":
            return
        d, _, i = self.more.partition(".")
        f = {"acct": self.view.get("acct"), "folder": "INBOX" if self.view["kind"] == "unified"
             else self.view.get("folder"), "before": (int(d), int(i))}
        n = self.st.page
        rows = (self.store.search(self.view["q"], limit=n, extra=LIST_EXTRA, **f) if self.view["kind"] == "search"
                else self.store.list(limit=n, extra=LIST_EXTRA, **f))
        self.more = f"{rows[-1]['date']}.{rows[-1]['id']}" if len(rows) == n else ""
        have = {it.row["msgid"] for it in self.items if it.row["msgid"]}
        self.items += self._arrange([Item(r) for r in rows if not (r["msgid"] and r["msgid"] in have)])

    def _arrange(self, items: list[Item]) -> list[Item]:
        if self.reg is not None and self.view["kind"] != "drafts":  # dedupe, snooze, ... from plugins
            dicts = [{"mi": it.row["msgid"], "item": it} for it in items]
            dicts = self.reg.filter_list(dicts, dict(self.view, kind=self.view["kind"]))
            items = []
            for d in dicts:
                d["item"].dup = d.get("dup", 0)
                items.append(d["item"])
        key = self.st.sort.lstrip("-")
        rev = self.st.sort.startswith("-")
        get = {"date": lambda it: (it.row["date"] or 0, it.id), "from": lambda it: (it.row["from_name"] or "").lower(),
               "subject": lambda it: re.sub(r"^((re|aw|fwd?|wg)\s*:\s*)+", "", (it.row["subject"] or "").lower()),
               "unread": lambda it: (it.row["unread"], it.row["date"] or 0)}.get(key)
        if get:
            items.sort(key=get, reverse=rev)
        if not self.st.threaded:
            return items
        groups: dict[str, list[Item]] = {}
        order: list[str] = []
        for it in items:
            t = it.row["thread"] or it.row["msgid"] or f"#{it.id}"
            if t not in groups:
                groups[t] = []
                order.append(t)
            groups[t].append(it)
        out = []
        for t in order:
            g = sorted(groups[t], key=lambda it: it.row["date"] or 0)
            for i, it in enumerate(g):
                it.tree = "" if i == 0 else ("└─ " if i == len(g) - 1 else "├─ ")
                out.append(it)
        return out

    def current(self) -> Item | None:
        return self.items[self.cur] if self.items and 0 <= self.cur < len(self.items) else None

    def targets(self) -> list[Item]:
        """Visual selection, else the open message, else the message under the cursor."""
        if self.anchor is not None and self.items:
            a, b = sorted((self.anchor, self.cur))
            return self.items[a:b + 1]
        if self.msg is not None and self.mode == "reader":
            return [it for it in self.items if it.id == self.msg["id"]] or [Item(self.msg)]
        it = self.current()
        return [it] if it else []

    # ---- list lines ------------------------------------------------------------------------------
    def list_line(self, it: Item) -> str:
        r = it.row
        if self.view["kind"] == "drafts":
            return f"{draft_ref(r['id'])}  {r['status']:9} {r['acct']:8} {term_safe(r['to_addr'], True)} | " \
                   f"{term_safe(r['subject'], True)}"
        flags = r["flags"] or ""
        fl = ("N" if r["unread"] else " ") + ("F" if "\\Flagged" in flags else "A" if "\\Answered" in flags else " ") + \
            ("@" if r["atts"] and r["atts"] != "[]" else " ")
        fields = _Fmt(flags=fl, date=fmt_date(r["date"], self.st), id=b36(r["id"]), acct=r["acct"],
                      folder=r["folder"], subject=term_safe(r["subject"], True) or "(no subject)",
                      to=term_safe(r["to_addr"], True), tree=it.tree,
                      dup=f"x{it.dup}" if it.dup else "", email=term_safe(r["from_addr"], True))
        fields["from"] = term_safe(r["from_name"] or r["from_addr"], True)
        try:
            return self.st.index_format.format_map(fields)
        except (ValueError, KeyError, IndexError) as e:
            return f"index_format error: {e}"

    # ---- reader ------------------------------------------------------------------------------------
    def open_current(self) -> None:
        it = self.current()
        if it:
            self.open(it.row if self.view["kind"] == "drafts" else self.store.get(it.id))

    def open(self, row) -> None:
        self.msg = row
        self.scroll = 0
        self.mode = "reader"
        self.render()
        if self.st.mark_read and row["unread"] and self.view["kind"] != "drafts":
            try:
                imapops.set_flag(self.store, self.cfg.accounts, [row["id"]], "read")
                self.msg = self.store.get(row["id"]) or row
                for it in self.items:
                    if it.id == row["id"]:
                        it.row = self.store.one(f"SELECT m.id, m.acct, m.folder, m.date, m.from_name, m.from_addr, "
                                                f"m.subject, m.unread, m.atts{LIST_EXTRA} FROM msgs m WHERE id=?",
                                                (row["id"],)) or it.row
            except Exception as e:
                self.say(f"could not mark read: {e}", error=True)

    def render(self, text: str | None = None) -> None:
        """Build the reader lines: headers, plugin badges/banners, body (cleaned or full)."""
        m = self.msg
        out: list[tuple[str, str]] = []
        if self.view["kind"] == "drafts":
            self.lines = [("normal", ln) for ln in term_safe(compose.preview(bytes(m["mime"]))).split("\n")]
            return
        name = term_safe(m["from_name"], True)
        addr = term_safe(m["from_addr"], True)
        out.append(("header", f"From:    {name} <{addr}>" if name and name != addr else f"From:    {addr}"))
        out.append(("header", f"To:      {term_safe(m['to_addr'], True)}"))
        if m["cc"]:
            out.append(("header", f"Cc:      {term_safe(m['cc'], True)}"))
        out.append(("header", f"Date:    {time.strftime('%Y-%m-%d %H:%M', time.localtime(m['date'] or 0))}"))
        out.append(("header", f"Subject: {term_safe(m['subject'], True)}"))
        atts = json.loads(m["atts"] or "[]")
        if atts:
            out.append(("header", "Attach:  " + ", ".join(f"{term_safe(n, True)} ({s} B)" for n, s in atts)))
        if self.reg is not None:
            rd = self.reg.render(m)
            if rd["badges"]:
                out.append(("badges", "  ".join(f"[{term_safe(b.get('text'), True)}]|{b.get('tone', 'neutral')}"
                                                for b in rd["badges"])))
            for b in rd["banners"]:
                out.append(("warn", "! " + term_safe(b.get("text"), True)))
        out.append(("normal", ""))
        if text is None:
            text = m["body"] if self.text_mode == "clean" else m["full"]
        body = term_safe(text or "")
        self.links = []
        seen = {}
        for url in URL_RE.findall(m["full"] or ""):
            if url not in seen:
                new, warn = self.reg.link(url, url) if self.reg is not None else (url, [])
                seen[url] = len(self.links) + 1
                self.links.append((new, warn))
        for ln in body.split("\n"):
            style = "quote" if ln.lstrip().startswith(">") else "normal"
            out.append((style, ln))
        self.lines = out

    def reader_lines(self, n: int) -> list[tuple[str, str]]:
        out = []
        for style, text in self.lines:
            if style in ("header", "badges", "warn"):
                out.append((style, text))
            else:
                out += [(style, w) for w in wrap(text, max(10, n))]
        return out

    # ---- messages ---------------------------------------------------------------------------------
    def say(self, text: str, error: bool = False) -> None:
        self.status, self.error = term_safe(text, True), error

    # ---- keys ---------------------------------------------------------------------------------------
    def key(self, key: str, source: str = "user") -> None:
        """Handle one key press (from the keyboard, or replayed from a macro)."""
        if key.startswith("\0line\0"):  # a command line recorded in a macro
            return self.execute(key[6:], source=source)
        if self.engine.recording is not None and source == "user":
            if not (self.engine.pending or self.engine.count or self.engine.special):
                self._seq_start = len(self.recorded)
            self.recorded.append(key)
        act = self.engine.feed(key, self.mode)
        if act is None:
            return
        if act.special == "stop":
            reg = self.engine.recording
            self.macros[reg] = self.recorded[:-1]  # without the final q
            self.engine.recording = None
            self.say(f"recorded @{reg} ({len(self.macros[reg])} keys)")
        elif act.special == "record":
            self.engine.recording, self.recorded = act.arg, []
            self.say(f"recording @{act.arg}")
        elif act.special == "play":
            reg = self.last_macro if act.arg == "@" else act.arg
            self.last_macro = reg
            for _ in range(act.count):
                for k in self.macros.get(reg, []):
                    self.key(k, source="replay")
        elif act.special == "mark":
            it = self.current()
            if it:
                self.marks[act.arg] = (dict(self.view), it.id)
                self.say(f"mark {act.arg}")
        elif act.special == "jump":
            self.jump(act.arg)
        else:
            self.execute(act.command, count=act.count, source=source)

    def jump(self, reg: str) -> None:
        if reg not in self.marks:
            return self.say(f"no mark {reg}", error=True)
        view, rid = self.marks[reg]
        if view != self.view:
            self.view = view
            self.load(reset=True)
        self.cur = next((i for i, it in enumerate(self.items) if it.id == rid), self.cur)

    # ---- commands -------------------------------------------------------------------------------------
    def execute(self, line: str, count: int = 1, source: str = "user") -> None:
        """Run a command line (pipeline). Errors go to the status line."""
        prev, self.source = self.source, source
        try:
            if line.startswith("prompt "):
                return self.prompt(line[7:])
            self.run_pipeline(line, count)
        except (CommandError, ValueError, LookupError, approve.ApprovalError, OSError) as e:
            self.say(str(e), error=True)
        except Exception as e:  # never crash the terminal
            self.say(f"{type(e).__name__}: {e}", error=True)
        finally:
            self.source = prev

    def run_pipeline(self, line: str, count: int = 1) -> None:
        """Every part works on the same messages; moves (move/archive/trash) run last."""
        parts = split_pipeline(line)
        targets = self.targets()
        moves = [p for p in parts if p[0] in ("move", "archive", "trash")]
        for p in [p for p in parts if p not in moves] + moves:
            name = p[0]
            if name not in self.commands:
                close = [c for c in self.commands if c.startswith(name)]
                if len(close) == 1:
                    name = close[0]
                else:
                    raise CommandError(f"unknown command: {name}" + (f" ({', '.join(close)}?)" if close else ""))
            fn = self.commands[name][0]
            fn(p[1:], targets, count)

    _seq_start = 0

    def prompt(self, what: str) -> None:
        """'command [prefill]', 'search', 'find': ask for a line, then run it. In a macro the typed
        line is recorded as a command line; replaying it runs with source 'replay' (cannot send)."""
        kind, _, prefill = what.partition(" ")
        if self.source != "user":
            raise CommandError("prompts need the keyboard")
        if kind == "command":
            line = self.ui.ask(":", prefill + (" " if prefill and not prefill.endswith(" ") else "")
                               if prefill else "", self.complete).strip()
        else:
            q = self.ui.ask("/").strip()
            line = (f"search {shlex.quote(q)}" if kind == "search" else f"find {shlex.quote(q)}") if q else ""
        if not line:
            return
        if self.engine.recording is not None:
            del self.recorded[self._seq_start:]
            self.recorded.append("\0line\0" + line)
        self.execute(line)

    def complete(self, line: str) -> list[str]:
        """Tab completion for the command line: command names, folders, settings."""
        parts = line.lstrip(":").split("|")[-1].lstrip()
        words = parts.split(" ")
        if len(words) <= 1:
            return sorted(c for c in self.commands if c.startswith(words[0]))
        cmd, arg = words[0], words[-1]
        pool: list[str] = []
        if cmd in ("move", "folder", "cd"):
            pool = sorted({f for a in self.cfg.accounts.values() for f in a.folders} |
                          {r["folder"] for r in self.store.stats()} | set(self.server_folders()))
            if cmd != "move":
                pool = [f"{a}/{f}" for a in self.cfg.accounts for f in pool] + pool
        elif cmd == "set":
            pool = [f"{k}=" for k in ("sort", "threaded", "layout", "sidebar", "text", "index_format",
                                      "mark_read", "split_ratio")]
        elif cmd == "mark":
            pool = list(imapops.FLAGS)
        elif cmd in ("send", "approve", "discard"):
            pool = [draft_ref(d["id"]) for d in self.store.pending()]
        return sorted(p for p in pool if p.startswith(arg))

    _folder_cache: list[str] | None = None

    def server_folders(self) -> list[str]:
        """Folder names on the servers (fetched once per session, for completion)."""
        if self._folder_cache is None:
            names: set[str] = set()
            for a in self.cfg.accounts.values():
                try:
                    names |= set(imapops.server_folders(a))
                except Exception:
                    pass
            self._folder_cache = sorted(names)
        return self._folder_cache

    def _builtin_commands(self) -> dict[str, tuple[Callable, str]]:
        c: dict[str, tuple[Callable, str]] = {}

        def cmd(name: str, help: str, *aliases: str):
            def deco(fn):
                for n in (name, *aliases):
                    c[n] = (fn, help)
                return fn
            return deco

        @cmd("quit", "leave mg tui", "q", "qa")
        def _quit(args, t, n):
            self.quit = True

        @cmd("down", "next message")
        def _down(args, t, n):
            self.move_cursor(n)

        @cmd("up", "previous message")
        def _up(args, t, n):
            self.move_cursor(-n)

        @cmd("top", "first message (or line N with a count)")
        def _top(args, t, n):
            self.cur = 0 if n == 1 else min(n - 1, len(self.items) - 1)

        @cmd("bottom", "last message")
        def _bottom(args, t, n):
            while self.more:
                self.load_more()
            self.cur = max(0, len(self.items) - 1)

        @cmd("pagedown", "half a page down")
        def _pd(args, t, n):
            self.move_cursor(10 * n)

        @cmd("pageup", "half a page up")
        def _pu(args, t, n):
            self.move_cursor(-10 * n)

        @cmd("open", "open the message (or folder)")
        def _open(args, t, n):
            if self.mode == "folders":
                label, view = self.folders[self.fcur]
                if view["kind"] != "header":
                    self.set_view(view)
                    self.mode = "normal"
                return
            self.open_current()

        @cmd("close", "close the reader")
        def _close(args, t, n):
            self.mode, self.msg = "normal", None

        @cmd("next", "open the next message")
        def _next(args, t, n):
            self.move_cursor(n)
            self.open_current()

        @cmd("prev", "open the previous message")
        def _prev(args, t, n):
            self.move_cursor(-n)
            self.open_current()

        @cmd("focus", "switch between folder list, message list and reader")
        def _focus(args, t, n):
            self.mode = {"folders": "normal", "normal": "reader" if self.msg is not None else "folders",
                         "reader": "normal"}.get(self.mode, "normal")
            if self.mode == "folders":
                self.load_folders()

        @cmd("folders", "go to the folder list")
        def _folders(args, t, n):
            self.load_folders()
            self.mode = "folders"

        @cmd("folder", "folder [ACCOUNT/]NAME: show a folder", "cd")
        def _folder(args, t, n):
            if not args:
                raise CommandError("folder name missing")
            acct, _, name = args[0].rpartition("/")
            acct = acct or self.view.get("acct") or self.cfg.default_account
            self.cfg.account(acct)
            self.set_view({"kind": "folder", "acct": acct, "folder": name})

        @cmd("unified", "all inboxes")
        def _unified(args, t, n):
            self.set_view({"kind": "unified"})

        @cmd("drafts", "drafts waiting for approval")
        def _drafts(args, t, n):
            self.set_view({"kind": "drafts"})

        @cmd("search", "search WORDS: full-text search")
        def _search(args, t, n):
            if not args:
                return self.set_view({"kind": "unified"})
            self.set_view({"kind": "search", "q": " ".join(args)})

        @cmd("visual", "start/stop selecting messages")
        def _visual(args, t, n):
            if self.anchor is None:
                self.anchor, self.mode = self.cur, "visual"
            else:
                self.anchor, self.mode = None, "normal"

        @cmd("mark", "mark read|unread|flag|unflag")
        def _mark(args, t, n):
            if not args or args[0] not in imapops.FLAGS:
                raise CommandError("mark read|unread|flag|unflag")
            self.server_op(t, args[0])

        @cmd("toggle", "toggle unread|flag")
        def _toggle(args, t, n):
            if not t:
                return
            row = t[0].row
            if args and args[0] == "flag":
                self.server_op(t, "unflag" if "\\Flagged" in (row["flags"] or "") else "flag")
            else:
                self.server_op(t, "read" if row["unread"] else "unread")

        @cmd("move", "move FOLDER: move to a folder on the server")
        def _move(args, t, n):
            if not args:
                raise CommandError("move FOLDER")
            self.move_op(t, target=" ".join(args))

        @cmd("archive", "move to the archive folder")
        def _archive(args, t, n):
            self.move_op(t, kind="archive")

        @cmd("trash", "move to Trash (never deletes for good)")
        def _trash(args, t, n):
            self.move_op(t, kind="trash")

        @cmd("sync", "fetch new mail")
        def _sync(args, t, n):
            self.sync()

        @cmd("set", "set KEY=VALUE (sort, threaded, layout, sidebar, text, ...); set KEY! toggles")
        def _set(args, t, n):
            for a in args:
                self.set_option(a)
            self.load()

        @cmd("map", "map MODE KEY COMMAND...: bind a key for this session")
        def _map(args, t, n):
            if len(args) < 3 or args[0] not in self.engine.keys:
                raise CommandError("map normal|reader|visual|folders KEY COMMAND")
            self.engine.keys[args[0]][args[1]] = " ".join(args[2:])
            self.say(f"mapped {args[1]}")

        @cmd("text", "toggle cleaned / full text")
        def _text(args, t, n):
            if self.msg is not None:
                self.text_mode = "full" if self.text_mode == "clean" else "clean"
                self.render()
                self.say(f"text: {self.text_mode}")

        @cmd("html", "show the HTML part with html_viewer (e.g. w3m -dump)")
        def _html(args, t, n):
            self.show_html()

        @cmd("links", "list the links of the message (tracking removed)")
        def _links(args, t, n):
            if self.msg is None:
                raise CommandError("open a message first")
            out = [("header", f"Links in: {term_safe(self.msg['subject'], True)}"), ("normal", "")]
            for i, (href, warn) in enumerate(self.links, 1):
                out.append(("link", f"[{i}] {term_safe(href, True)}"))
                out += [("warn", f"    ! {term_safe(w, True)}") for w in warn]
            out += [("normal", ""), ("dim", ":open N opens a link, T or :text goes back")]
            self.lines, self.scroll, self.mode = out, 0, "reader"

        @cmd("open-link", "open-link N: open link N in the browser", "ol")
        def _openlink(args, t, n):
            i = int(args[0]) if args else n
            if not 1 <= i <= len(self.links):
                raise CommandError(f"no link {i}")
            self.open_url(self.links[i - 1][0])

        @cmd("reply", "reply (reply all with: reply all)")
        def _reply(args, t, n):
            self.compose("all" if args and args[0] == "all" else "reply")

        @cmd("forward", "forward the message")
        def _forward(args, t, n):
            self.compose("forward")

        @cmd("compose", "write a new message", "new")
        def _compose(args, t, n):
            self.compose("new")

        @cmd("send", "send dN: send a pending draft (asks you to type the id)", "approve")
        def _send(args, t, n):
            ref = args[0] if args else (draft_ref(t[0].id) if t and self.view["kind"] == "drafts" else "")
            if not ref:
                raise CommandError("send dN")
            self.send(parse_id(ref, draft=True))

        @cmd("discard", "discard dN: discard a pending draft")
        def _discard(args, t, n):
            ref = args[0] if args else (draft_ref(t[0].id) if t and self.view["kind"] == "drafts" else "")
            if not ref:
                raise CommandError("discard dN")
            self.say(approve.reject_draft(self.store, parse_id(ref, draft=True), via="tui"))
            self.load()

        @cmd("scroll", "scroll N|page|half|-half|top|bottom (reader)")
        def _scroll(args, t, n):
            a = args[0] if args else "1"
            page = 20
            step = {"page": page, "half": page // 2, "-half": -page // 2, "-page": -page}.get(a)
            if a == "top":
                self.scroll = 0
            elif a == "bottom":
                self.scroll = 10 ** 6
            else:
                self.scroll = max(0, self.scroll + (step if step is not None else int(a)) * n)

        @cmd("find", "find TEXT: search in the open message (n/N for more)")
        def _find(args, t, n):
            self.find = " ".join(args)
            self.findnext(1)

        @cmd("findnext", "next match of / in the reader")
        def _fn(args, t, n):
            self.findnext(1)

        @cmd("findprev", "previous match of / in the reader")
        def _fp(args, t, n):
            self.findnext(-1)

        @cmd("help", "list commands and keys")
        def _help(args, t, n):
            out = [("header", "Commands (:name), | chains them, moves run last"), ("normal", "")]
            seen = set()
            for name, (fn, h) in sorted(self.commands.items()):
                if fn in seen:
                    continue
                seen.add(fn)
                out.append(("normal", f"  {name:12} {h}"))
            out += [("normal", ""), ("header", "Keys"), ("normal", "")]
            for mode, keys in self.engine.keys.items():
                out.append(("header", f"  {mode}"))
                out += [("normal", f"    {k:10} {v}") for k, v in sorted(keys.items())]
            out += [("normal", ""), ("normal", "  m{a-z} set mark, '{a-z} jump, q{a-z} record macro, q stop, "
                                               "@{a-z} play, @@ repeat, counts like 5j")]
            self.lines, self.scroll, self.mode = out, 0, "reader"

        return c

    def _plugin_cmd(self, name: str) -> Callable:
        api, fn, _ = self.reg.tui_commands[name]

        def run(args, targets, count):
            from .plugin import Msg
            res = self.reg.call(api, fn, PluginTUI(self, api), " ".join(args), [Msg(api, self.store.get(it.id)) for it in targets
                                                                         if self.view["kind"] != "drafts"])
            if res:
                self.say(str(res))
            self.load()
        return run

    # ---- actions -------------------------------------------------------------------------------------
    def move_cursor(self, d: int) -> None:
        if self.mode == "folders":
            self.fcur = max(0, min(len(self.folders) - 1, self.fcur + d))
            while 0 < self.fcur < len(self.folders) - 1 and self.folders[self.fcur][1]["kind"] == "header":
                self.fcur += 1 if d > 0 else -1
            return
        self.cur = max(0, min(len(self.items) - 1, self.cur + d))
        if self.cur > len(self.items) - 30 and self.more:
            self.load_more()

    def set_view(self, view: dict) -> None:
        self.view, self.anchor, self.msg = view, None, None
        if self.mode in ("reader", "visual"):
            self.mode = "normal"
        self.load(reset=True)
        self.say(f"{self.view_title()}: {len(self.items)}{'+' if self.more else ''} messages")

    def set_option(self, a: str) -> None:
        key, eq, val = a.partition("=")
        if key.endswith("!"):
            key = key[:-1]
            cur = getattr(self.st, key, None)
            if not isinstance(cur, bool):
                raise CommandError(f"{key} is not a switch")
            setattr(self.st, key, not cur)
            return self.say(f"{key}={'on' if not cur else 'off'}")
        if key not in ("sort", "threaded", "layout", "sidebar", "text", "index_format", "mark_read",
                       "split_ratio", "date_format"):
            raise CommandError(f"unknown option {key}")
        cur = getattr(self.st, key)
        if isinstance(cur, bool):
            v = val.lower() in ("1", "true", "on", "yes") if eq else True
        elif isinstance(cur, float):
            v = float(val)
        else:
            v = val
        if key == "layout" and v not in ("split", "vsplit", "full"):
            raise CommandError("layout split|vsplit|full")
        setattr(self.st, key, v)
        if key == "text":
            self.text_mode = v

    def server_op(self, targets: list[Item], op: str) -> None:
        if self.view["kind"] == "drafts" or not targets:
            return
        n = imapops.set_flag(self.store, self.cfg.accounts, [it.id for it in targets], op)
        self.load()
        if self.msg is not None and self.mode == "reader":
            self.msg = self.store.get(self.msg["id"]) or self.msg
        self.anchor = None if self.mode != "visual" else self.anchor
        self.say(f"{n} marked {op}")

    def move_op(self, targets: list[Item], target: str | None = None, kind: str | None = None) -> None:
        if self.view["kind"] == "drafts" or not targets:
            return
        idx = self.cur
        n, dest = imapops.move(self.store, self.cfg.accounts, [it.id for it in targets], target=target, kind=kind)
        self.anchor, self.msg = None, None
        self.mode = "normal"
        self.load()
        self.cur = min(idx, max(0, len(self.items) - 1))
        self.say(f"{n} moved to {dest}")

    def sync(self) -> None:
        if self.syncing:
            return self.say("sync already running")
        self.syncing = True
        self.say("syncing...")

        def run():
            from . import imapsync
            from .syncer import process_new
            store = Store(self.db)
            try:
                total = sum(imapsync.sync_account(store, a, None, self.cfg.max_raw_bytes, self.cfg.initial_days,
                                                  log=lambda s: None) for a in self.cfg.accounts.values())
                process_new(self.cfg, store, self.reg, unattended=False)
                self.say(f"sync done: {total} new")
            except Exception as e:
                self.say(f"sync failed: {e}", error=True)
            finally:
                self.syncing = False
                self.after_sync = True
        threading.Thread(target=run, daemon=True).start()

    after_sync = False

    def findnext(self, d: int) -> None:
        if not self.find:
            return
        lines = [t for _, t in self.lines]
        rng = list(range(self.scroll + 1, len(lines))) + list(range(0, self.scroll + 1)) if d > 0 else \
            list(range(self.scroll - 1, -1, -1)) + list(range(len(lines) - 1, self.scroll - 1, -1))
        for i in rng:
            if self.find.lower() in lines[i].lower():
                self.scroll = i
                return self.say(f"/{self.find}")
        self.say(f"not found: {self.find}", error=True)

    def show_html(self) -> None:
        if self.msg is None:
            raise CommandError("open a message first")
        from .webui import parse_raw, sanitize
        markup = html_body(parse_raw(self.cfg, self.msg))
        if not markup:
            raise CommandError("no HTML part")
        if not self.st.html_viewer:
            from .clean import html_to_text, tidy
            return self.render(tidy(html_to_text(markup, drop_quotes=False)))
        doc, _ = sanitize(markup, link=self.reg.link if self.reg is not None else None)
        fd, path = tempfile.mkstemp(suffix=".html", prefix="mg-")  # mode 0600
        try:
            with os.fdopen(fd, "w") as f:
                f.write(doc)
            argv = [a.replace("{file}", path) for a in self.st.html_viewer]
            if "{file}" not in " ".join(self.st.html_viewer):
                argv.append(path)
            r = subprocess.run(argv, capture_output=True, text=True, timeout=30)  # nosec B603: argv from tui.toml
            self.render(r.stdout)
        finally:
            os.unlink(path)

    def open_url(self, url: str) -> None:
        if not url.lower().startswith(("http://", "https://", "mailto:")):
            raise CommandError("only http(s) and mailto links are opened")
        import sys
        if not self.st.opener and not os.environ.get("BROWSER") and sys.platform == "win32":
            import webbrowser
            webbrowser.open(url)  # os.startfile, no shell
            return self.say(f"opened {url}")
        argv = list(self.st.opener) or (shlex.split(os.environ["BROWSER"]) if os.environ.get("BROWSER") else
                                        ["open"] if sys.platform == "darwin" else ["xdg-open"])
        subprocess.Popen(argv + [url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,  # nosec B603
                         start_new_session=True)
        self.say(f"opened {url}")

    # ---- compose and send ---------------------------------------------------------------------------
    def template(self, mode: str) -> tuple[str, dict]:
        """Editor template and context (account, reply/forward reference)."""
        acct = self.cfg.account()
        ctx: dict = {"mode": mode, "ref": None}
        to = cc = subject = body = ""
        if mode != "new":
            t = self.targets()
            if not t or self.view["kind"] == "drafts":
                raise CommandError("no message selected")
            m = self.store.get(t[0].id)
            acct = self.cfg.account(m["acct"])
            ctx["ref"] = m["id"]
            who = formataddr((m["from_name"], m["from_addr"])) if m["from_name"] != m["from_addr"] else m["from_addr"]
            date = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["date"] or 0))
            if mode in ("reply", "all"):
                f = compose.reply_fields(acct, m, mode == "all")
                to, cc, subject = ", ".join(f["to"]), ", ".join(f["cc"]), f["subject"]
                intro = self.st.quote_intro.format_map(_Fmt({"date": date, "from": who}))
                body = "\n\n" + term_safe(intro) + "\n" + "\n".join("> " + ln for ln in term_safe(m["full"]).split("\n"))
            else:
                subject = m["subject"] if re.match(r"^\s*(fwd?|wg)\s*:", m["subject"] or "", re.I) \
                    else f"Fwd: {m['subject']}"
                body = (f"\n\n---------- Forwarded message ----------\nFrom: {who}\nDate: {date}\n"
                        f"Subject: {m['subject']}\nTo: {m['to_addr']}\n\n{term_safe(m['full'])}")
        head = [f"From: {acct.name} <{acct.email}>", f"To: {term_safe(to, True)}", f"Cc: {term_safe(cc, True)}",
                "Bcc: ", f"Subject: {term_safe(subject, True)}", "Attach: "]
        text = ("# mg tui: edit, save and quit. Empty To or empty file = cancel. Lines starting with # are "
                "ignored.\n# Attach: comma-separated file paths. Your signature is added when sending.\n"
                + "\n".join(head) + "\n" + term_safe(body) + "\n")
        return text, ctx

    def parse_template(self, text: str) -> dict:
        lines = [ln for ln in text.split("\n") if not ln.startswith("#")]
        hdr: dict[str, str] = {}
        i = 0
        while i < len(lines) and lines[i].strip():
            k, sep, v = lines[i].partition(":")
            if not sep:
                break
            hdr[k.strip().lower()] = v.strip()
            i += 1
        body = "\n".join(lines[i + 1:] if i < len(lines) else []).strip("\n")
        return {"from": hdr.get("from", ""), "to": hdr.get("to", ""), "cc": hdr.get("cc", ""),
                "bcc": hdr.get("bcc", ""), "subject": hdr.get("subject", ""),
                "attach": [a.strip() for a in hdr.get("attach", "").split(",") if a.strip()], "body": body}

    def compose(self, mode: str) -> None:
        text, ctx = self.template(mode)
        edited = self.ui.edit(text)
        if not edited or not edited.strip():
            return self.say("cancelled")
        f = self.parse_template(edited)
        if not compose.addr_list([f["to"]]):
            return self.say("no recipient, cancelled")
        name = f["from"].split("<")[0].strip()
        acct = self.cfg.accounts.get(name) or next((a for a in self.cfg.accounts.values()
                                                    if a.email in f["from"]), self.cfg.account())
        orig = self.store.get(ctx["ref"]) if ctx["ref"] else None
        irt = refs = ""
        if mode in ("reply", "all") and orig:
            irt, refs = orig["msgid"] or "", orig["refs"] or ""
        att_data = []
        if mode == "forward" and orig:
            from .clean import attachment_parts
            from .webui import parse_raw
            att_data = [(p.get_filename() or "unnamed", p.get_payload(decode=True) or b"")
                        for p in attachment_parts(parse_raw(self.cfg, orig))]
        mime, rcpts = compose.build(acct, compose.addr_list([f["to"]]), f["subject"], f["body"],
                                    compose.addr_list([f["cc"]]), compose.addr_list([f["bcc"]]), in_reply_to=irt,
                                    references=refs, attach=[os.path.expanduser(a) for a in f["attach"]],
                                    attach_data=att_data)
        q = approve.create_draft(self.cfg, self.store, acct, mime, rcpts,
                                 reply_msg=orig["id"] if mode in ("reply", "all") and orig else None,
                                 via="tui", manual=True, notify=False)
        ref = draft_ref(q.did)
        if self.source == "user" and self.confirm_send(q.did, f"Send now? Type {ref} to send, Enter keeps it "
                                                               f"as a draft for approval: "):
            res = approve.send_draft(self.cfg, self.store, q.did, via="tui")
            if mode in ("reply", "all") and orig:
                try:
                    imapops.set_flag(self.store, self.cfg.accounts, [orig["id"]], "answered")
                except Exception:
                    pass
            self.say(res)
        else:
            approve.notify_pending(self.cfg, self.store, q.did)
            self.say(f"{ref} queued, waiting for approval (mg queue, ntfy, :send {ref})")
        self.load()

    def confirm_send(self, did: int, prompt: str) -> bool:
        """The only way to send from the TUI: the human types the draft id at the real keyboard."""
        from .plugin import assert_not_plugin
        assert_not_plugin("send from mg tui")
        if self.source != "user":
            raise CommandError("sending needs you at the keyboard (not from a macro, on_start or a plugin)")
        if not self.ui.tty:
            raise CommandError("sending needs an interactive terminal")
        return self.ui.ask(prompt).strip().lower() == draft_ref(did)

    def send(self, did: int) -> None:
        from .plugin import assert_not_plugin
        assert_not_plugin("send from mg tui")
        if self.source != "user":
            raise CommandError("sending needs you at the keyboard (not from a macro, on_start or a plugin)")
        if not self.ui.tty:
            raise CommandError("sending needs an interactive terminal")
        d = self.store.draft(did)
        if not d or d["status"] != "pending":
            raise CommandError(f"{draft_ref(did)}: no pending draft")
        if self.view["kind"] != "drafts":
            self.set_view({"kind": "drafts"})
            self.cur = next((i for i, it in enumerate(self.items) if it.id == did), 0)
        self.mode, self.msg = "reader", d  # show the exact draft while asking
        try:
            self.render()
            self.ui.redraw()
            if self.confirm_send(did, f"Send {draft_ref(did)} to {term_safe(d['to_addr'], True)}? "
                                      f"Type {draft_ref(did)} to send: "):
                self.say(approve.send_draft(self.cfg, self.store, did, via="tui"))
            else:
                self.say("not sent")
        finally:
            self.mode, self.msg = "normal", None
            self.load()

    # ---- status line --------------------------------------------------------------------------------
    def statusline(self) -> tuple[str, str]:
        mode = {"normal": "LIST", "reader": "READ", "visual": "VISUAL", "folders": "FOLDERS"}[self.mode]
        if self.engine.recording is not None:
            mode += f" @{self.engine.recording}"
        sel = f" {abs(self.cur - self.anchor) + 1} selected" if self.anchor is not None else ""
        left = f" {mode} | {self.view_title()} [{self.cur + 1 if self.items else 0}/{len(self.items)}" \
               f"{'+' if self.more else ''}]{sel}"
        segs = []
        if self.reg is not None:
            for api, fn in self.reg.tui_status:
                s = self.reg.call(api, fn, PluginTUI(self, api))
                if s:
                    segs.append(term_safe(str(s), True))
        pend = len(self.store.pending())
        if pend:
            segs.append(f"{pend} to approve")
        if self.syncing:
            segs.append("syncing")
        return left, " | ".join(segs)


class PluginTUI:
    """The narrow object plugin tui commands and status segments get."""

    def __init__(self, c: Controller, api):
        self._c, self._api = c, api

    def status(self, text: str) -> None:
        self._c.say(text)

    def current(self):
        from .plugin import Msg
        it = self._c.current()
        if not it or self._c.view["kind"] == "drafts":
            return None
        return Msg(self._api, self._c.store.get(it.id))

    def selection(self):
        from .plugin import Msg
        if self._c.view["kind"] == "drafts":
            return []
        return [Msg(self._api, self._c.store.get(it.id)) for it in self._c.targets()]

    @property
    def mode(self) -> str:
        return self._c.mode

    @property
    def view(self) -> dict:
        return dict(self._c.view)

    def run(self, line: str) -> None:
        """Run a command line. Anything that would send is refused (it needs the human)."""
        self._c.execute(line, source="plugin")

    def confirm(self, question: str) -> bool:
        """Yes/no typed at the real keyboard; always False when not driven by the human."""
        if self._c.source not in ("user",):
            return False
        return self._c.ui.ask(f"{question} [y/N] ").strip().lower() in ("y", "yes", "j", "ja")
