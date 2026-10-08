"""Plugin system, hook API version 1. See PLUGINS.md.

A plugin is a Python module with `api_version = 1` and `setup(mg)`. `mg` is a PluginAPI:
plugins register hooks with its decorators and act through its methods. Plugins can read
mail, add metadata, badges and rule conditions/actions, create drafts, mark and move mail,
notify, and add CLI commands and UI views. They can never send or approve: every send path
in mailgate checks `assert_not_plugin()`, and drafts created by plugins always wait for a
human, whatever the approval mode.

This module is imported by `mg ls` (through smtpsend), so keep its top level cheap.
"""
from __future__ import annotations

import contextvars
import json
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

API_VERSION = 1
GROUP = "mailgate.plugins"  # entry point group for pip-installed plugins
_active: contextvars.ContextVar[str | None] = contextvars.ContextVar("mailgate_plugin", default=None)


class PluginError(ValueError):
    """Plugin could not be loaded or used."""


class PluginSendError(PermissionError):
    """A plugin tried to send or approve mail."""


def active() -> str | None:
    """Name of the plugin whose code is running right now (in this thread), else None."""
    return _active.get()


def assert_not_plugin(what: str) -> None:
    """Called by every send/approve path. Plugins may create drafts, never send them."""
    if name := _active.get():
        raise PluginSendError(f"plugin {name!r} tried to {what}; plugins can only create drafts for approval")


def _log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, file=sys.stderr, flush=True)


@dataclass
class UIAction:
    plugin: str
    id: str
    label: Any  # str or {"en": ..., "de": ...}
    fn: Callable
    choices: list | None = None  # [(value, label), ...] -> small menu in the UI
    when: Callable | None = None  # when(msg) -> bool: show the button for this message
    confirm: Any = None
    icon: str = ""


@dataclass
class UIView:
    plugin: str
    id: str
    label: Any
    fn: Callable  # fn(query: dict) -> {"items": [...], "empty": str}
    actions: list = field(default_factory=list)  # [UIAction] working on selected item keys
    badge: Callable | None = None  # badge() -> int
    icon: str = "folder"


@dataclass
class Command:
    plugin: str
    name: str
    help: str
    fn: Callable  # fn(args)
    args: Callable | None = None  # args(argparse.ArgumentParser)


class Msg:
    """Read-only view of a cached message for plugins."""

    def __init__(self, api: "PluginAPI", row):
        from .store import b36
        self._api, self._row, self._email = api, row, None
        self.rowid, self.id = row["id"], b36(row["id"])
        for k in ("acct", "folder", "uid", "msgid", "thread", "irt", "refs", "from_name", "from_addr", "cc",
                  "subject", "date", "unread", "flags", "body", "full", "reply_to"):
            setattr(self, k, row[k])
        self.to = row["to_addr"]
        self.flagged = "\\Flagged" in (row["flags"] or "")
        self.atts = [(n, s) for n, s in json.loads(row["atts"] or "[]")]

    def email(self):
        """The parsed EmailMessage (fetched read-only from IMAP if it was too big to cache)."""
        if self._email is None:
            from email import policy
            from email.parser import BytesParser
            raw = self._row["raw"]
            if raw is None:
                from . import imapsync
                raw = imapsync.fetch_raw(self._api._reg.cfg.account(self.acct), self.folder, self.uid)
            self._email = BytesParser(policy=policy.default).parsebytes(bytes(raw))
        return self._email

    def header(self, name: str) -> str:
        if self._row["raw"] is None and self._email is None:
            return ""
        return " ".join(str(self.email().get(name, "")).split())

    def html(self) -> str | None:
        from .clean import html_body
        return html_body(self.email())

    def meta(self, plugin: str | None = None) -> dict:
        return self._api.meta_get(self, plugin)


class PluginAPI:
    """What a plugin gets. Registration decorators plus a narrow set of actions."""

    def __init__(self, name: str, reg: "Registry", settings: dict):
        self.name, self._reg, self.settings = name, reg, dict(settings)
        self._phase = ""  # "synced": local-only phase, no server changes
        self._tl = threading.local()

    # ---- registration -------------------------------------------------------------------
    def _add(self, hook: str, fn: Callable) -> Callable:
        self._reg.hooks.setdefault(hook, []).append((self, fn))
        return fn

    def on_message_synced(self, fn):
        """fn(msg) -> dict | None, for every newly cached message (also on `mg sync`). Local work only:
        the returned dict is stored as this plugin's metadata for the message."""
        return self._add("synced", fn)

    def on_new_mail(self, fn):
        """fn(msg) after a background sync (mg ui / mg daemon --sync), after the sorting rules."""
        return self._add("new_mail", fn)

    def on_render(self, fn):
        """fn(msg) -> {"badges": [{"text", "tone", "title"}], "banners": [{"text", "tone"}],
        "lines": [str]} | None. Badges/banners appear in the UI, lines in `mg read`."""
        return self._add("render", fn)

    def link_filter(self, fn):
        """fn(href, text) -> (href, warning | None). Runs for every link the UI shows."""
        return self._add("link", fn)

    def list_filter(self, fn):
        """fn(items, view) -> items. items are message list dicts (id, msgid, f, a, ...); view is
        {"kind": unified|folder|search, "acct", "folder", "q"}."""
        return self._add("list", fn)

    def every(self, minutes: float):
        """Decorator: fn() every N minutes while mg ui or mg daemon --sync runs."""
        def deco(fn):
            self._reg.ticks.append([self, fn, minutes * 60, 0.0])
            return fn
        return deco

    def rule_condition(self, key: str):
        """Decorator: fn(msg, value) -> bool for `key = value` in a [[rules]] entry."""
        def deco(fn):
            self._reg.conditions[key] = (self, fn)
            return fn
        return deco

    def rule_action(self, kind: str, options: tuple = ()):
        """Decorator: fn(msg, arg, rule) for `action = "kind:arg"`; options are extra rule keys it reads."""
        def deco(fn):
            self._reg.actions[kind] = (self, fn, set(options))
            return fn
        return deco

    def command(self, name: str, help: str, args: Callable | None = None):
        """Decorator: `mg <name>`; fn(args). args(parser) adds argparse arguments."""
        def deco(fn):
            self._reg.commands[name] = Command(self.name, name, help, fn, args)
            return fn
        return deco

    def ui_action(self, id: str, label, choices=None, when=None, confirm=None, icon: str = ""):
        """Decorator: a button on an open message; fn(msg, choice) -> str (status text)."""
        def deco(fn):
            self._reg.ui_actions.append(UIAction(self.name, id, label, fn, choices, when, confirm, icon))
            return fn
        return deco

    def ui_view(self, id: str, label, badge: Callable | None = None, icon: str = "folder"):
        """Decorator: a sidebar view; fn(query) -> {"items": [{"key", "title", "sub", "meta", "msg"}], ...}.
        Add buttons with view_action(view_id, ...)."""
        def deco(fn):
            self._reg.ui_views.append(UIView(self.name, id, label, fn, [], badge, icon))
            return fn
        return deco

    def view_action(self, view_id: str, id: str, label, choices=None, confirm=None):
        """Decorator: a button in a sidebar view; fn(keys: list[str], choice) -> str."""
        def deco(fn):
            for v in self._reg.ui_views:
                if v.plugin == self.name and v.id == view_id:
                    v.actions.append(UIAction(self.name, id, label, fn, choices, None, confirm))
                    return fn
            raise PluginError(f"{self.name}: view {view_id!r} must be registered first")
        return deco

    # ---- data -------------------------------------------------------------------------------
    def _store(self):
        from .store import Store
        if getattr(self._tl, "store", None) is None:
            self._tl.store = Store(self._reg.db)
        return self._tl.store

    def q(self, sql: str, args: tuple | list = ()) -> list:
        """Read-only SQL over the cache (tables msgs, folders, drafts, audit, ...)."""
        if not sql.lstrip().lower().startswith(("select", "with")):
            raise PluginError("q() is read-only; use the API methods to change things")
        import sqlite3
        if getattr(self._tl, "ro", None) is None:
            self._tl.ro = sqlite3.connect(f"file:{self._reg.db}?mode=ro", uri=True, timeout=30)
            self._tl.ro.row_factory = sqlite3.Row
        return self._tl.ro.execute(sql, args).fetchall()

    def message(self, ref) -> Msg | None:
        from .store import parse_id
        row = self._store().get(parse_id(ref) if isinstance(ref, str) else int(ref))
        return Msg(self, row) if row else None

    def messages(self, where: str = "1", args: tuple | list = (), limit: int = 200) -> list[Msg]:
        return [Msg(self, r) for r in self.q(f"SELECT * FROM msgs WHERE {where} ORDER BY date DESC LIMIT ?",
                                             [*args, limit])]

    def meta_get(self, msg: Msg, plugin: str | None = None) -> dict:
        r = self._store().one("SELECT data FROM plugin_meta WHERE msgid=? AND plugin=?",
                              (msg.msgid or f"#{msg.rowid}", plugin or self.name))
        return json.loads(r[0]) if r else {}

    def meta_set(self, msg: Msg, data: dict) -> None:
        self._store().db.execute("INSERT OR REPLACE INTO plugin_meta VALUES(?,?,?)",
                                 (msg.msgid or f"#{msg.rowid}", self.name, json.dumps(data, ensure_ascii=False)))

    def data_get(self, kind: str, key: str, default=None):
        r = self._store().one("SELECT data FROM plugin_data WHERE plugin=? AND kind=? AND key=?",
                              (self.name, kind, key))
        return json.loads(r[0]) if r else default

    def data_set(self, kind: str, key: str, value) -> None:
        self._store().db.execute("INSERT OR REPLACE INTO plugin_data VALUES(?,?,?,?,?)",
                                 (self.name, kind, key, json.dumps(value, ensure_ascii=False), int(time.time())))

    def data_del(self, kind: str, key: str) -> None:
        self._store().db.execute("DELETE FROM plugin_data WHERE plugin=? AND kind=? AND key=?", (self.name, kind, key))

    def data_list(self, kind: str) -> list[tuple[str, Any]]:
        return [(r[0], json.loads(r[1])) for r in self._store().q(
            "SELECT key, data FROM plugin_data WHERE plugin=? AND kind=? ORDER BY ts", (self.name, kind))]

    # ---- actions ----------------------------------------------------------------------------
    @property
    def accounts(self) -> list[dict]:
        """Account names and addresses (no server settings, no passwords)."""
        return [{"name": a.name, "email": a.email, "display_name": a.display_name}
                for a in self._reg.cfg.accounts.values()]

    def _server_ok(self) -> None:
        if self._phase == "synced":
            raise PluginError("on_message_synced is local-only; use on_new_mail or a rule action to change mail")

    def mark(self, msgs: list, op: str) -> int:
        """op: read | unread | flag | unflag | answered (on the IMAP server)."""
        self._server_ok()
        from . import imapops
        return imapops.set_flag(self._store(), self._reg.cfg.accounts, [m.rowid for m in msgs], op)

    def move(self, msgs: list, folder: str | None = None, kind: str | None = None) -> str:
        """Move to a folder, or kind='archive'/'trash'. Never deletes permanently."""
        self._server_ok()
        from . import imapops
        return imapops.move(self._store(), self._reg.cfg.accounts, [m.rowid for m in msgs], folder, kind)[1]

    def draft(self, to: list[str], subject: str, body: str, acct: str | None = None, cc: list[str] | None = None,
              attach_data: list[tuple[str, bytes]] | None = None, reply_to: Msg | None = None,
              signature: bool = True) -> str:
        """Queue a draft. It ALWAYS waits for human approval, whatever the approval mode. Returns 'd7'."""
        from . import approve, compose
        cfg = self._reg.cfg
        account = cfg.account(acct or (reply_to.acct if reply_to else None))
        irt = refs = ""
        if reply_to:
            irt, refs = reply_to.msgid or "", reply_to.refs or ""
        mime, rcpts = compose.build(account, compose.addr_list(to), subject, body, compose.addr_list(cc or []),
                                    in_reply_to=irt, references=refs, attach_data=attach_data, signature=signature)
        q = approve.create_draft(cfg, self._store(), account, mime, rcpts,
                                 reply_msg=reply_to.rowid if reply_to else None, via=f"plugin:{self.name}",
                                 manual=True)
        from .store import draft_ref
        return draft_ref(q.did)

    def notify(self, text: str, title: str = "mailgate", priority: int = 3) -> None:
        """Push to ntfy (if configured), the UI (if open) and the event stream."""
        self.emit("notify", {"text": text, "title": title})
        self._reg.publish({"t": "notify", "plugin": self.name, "text": text})
        cfg = self._reg.cfg
        if cfg.ntfy:
            from . import approve
            try:
                approve.ntfy_publish(cfg, {"topic": cfg.ntfy.topic, "title": title, "message": text,
                                           "priority": priority, "tags": ["email"]})
            except Exception as e:
                self.log(f"ntfy failed: {e}")

    def emit(self, type_: str, data: dict) -> None:
        """Add an event to the local stream (`mg events`) as plugin.<name>.<type>."""
        self._store().emit(f"plugin.{self.name}.{type_}", data)

    def log(self, msg: str) -> None:
        _log(f"plugin {self.name}: {msg}")


@dataclass
class Loaded:
    name: str
    source: str
    description: str = ""
    error: str = ""


class Registry:
    """Loaded plugins and their hooks for one process."""

    def __init__(self, cfg, db, publish: Callable[[dict], None] | None = None):
        self.cfg, self.db = cfg, db
        self.publish = publish or (lambda ev: None)
        self.loaded: dict[str, Loaded] = {}
        self.apis: dict[str, PluginAPI] = {}
        self.hooks: dict[str, list] = {}
        self.ticks: list[list] = []
        self.conditions: dict[str, tuple] = {}
        self.actions: dict[str, tuple] = {}
        self.commands: dict[str, Command] = {}
        self.ui_actions: list[UIAction] = []
        self.ui_views: list[UIView] = []

    def call(self, api: PluginAPI, fn: Callable, *args, default=None, phase: str = ""):
        """Run plugin code with the send guard set; errors are logged, never raised into the core."""
        token = _active.set(api.name)
        api._phase = phase
        try:
            return fn(*args)
        except PluginSendError as e:  # never let it through, never crash the caller
            api.log(f"BLOCKED: {e}")
            return default
        except Exception as e:
            api.log(f"{getattr(fn, '__name__', 'hook')} failed: {type(e).__name__}: {e}")
            return default
        finally:
            api._phase = ""
            _active.reset(token)

    def has(self, hook: str) -> bool:
        return bool(self.hooks.get(hook))

    # ---- hook runners ---------------------------------------------------------------------
    def synced(self, rows) -> None:
        for api, fn in self.hooks.get("synced", []):
            for r in rows:
                m = Msg(api, r)
                data = self.call(api, fn, m, phase="synced")
                if isinstance(data, dict):
                    api.meta_set(m, data)

    def new_mail(self, rows) -> None:
        for api, fn in self.hooks.get("new_mail", []):
            for r in rows:
                self.call(api, fn, Msg(api, r))

    def render(self, row) -> dict:
        out: dict = {"badges": [], "banners": [], "lines": []}
        for api, fn in self.hooks.get("render", []):
            res = self.call(api, fn, Msg(api, row)) or {}
            for k in out:
                out[k] += [x | {"plugin": api.name} if isinstance(x, dict) else x for x in res.get(k, [])]
        return out

    def link(self, href: str, text: str = "") -> tuple[str, list[str]]:
        warnings = []
        for api, fn in self.hooks.get("link", []):
            res = self.call(api, fn, href, text)
            if res:
                href = res[0] or href
                if res[1]:
                    warnings.append(res[1])
        return href, warnings

    def filter_list(self, items: list[dict], view: dict) -> list[dict]:
        for api, fn in self.hooks.get("list", []):
            res = self.call(api, fn, items, view)
            if isinstance(res, list):
                items = res
        return items

    def tick(self) -> None:
        now = time.time()
        for t in self.ticks:
            api, fn, every, last = t
            if now - last >= every:
                t[3] = now
                self.call(api, fn)


# ---- loading ------------------------------------------------------------------------------------

def plugin_dir():
    from .config import config_path
    return config_path().parent / "plugins"


def _bundled() -> dict[str, str]:
    import pkgutil
    from . import plugins
    return {m.name: f"mailgate.plugins.{m.name}" for m in pkgutil.iter_modules(plugins.__path__)}


def _entry_points() -> dict:
    from importlib.metadata import entry_points
    return {ep.name: ep for ep in entry_points(group=GROUP)}


def pins_path():
    return plugin_dir().parent / "plugins.lock"


def read_pins() -> dict[str, str]:
    try:
        return json.loads(pins_path().read_text())
    except (OSError, ValueError):
        return {}


def _sha256(path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_owner(path) -> None:
    """Refuse files/dirs that others could change (POSIX): must be ours and not group/world-writable."""
    import os
    import stat
    if os.name != "posix":
        return
    st = path.stat()
    if st.st_uid != os.getuid():
        raise PluginError(f"{path} is not owned by you")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PluginError(f"{path} is writable by group/others (chmod go-w {path})")


def locate(name: str):
    """(kind, path or entry point) of a plugin without importing it. kind: bundled | file | package."""
    import importlib.util
    if not name.isidentifier():
        raise PluginError(f"bad plugin name {name!r}")
    if importlib.util.find_spec(f"mailgate.plugins.{name}") is not None:
        return "bundled", f"mailgate.plugins.{name}"
    f = plugin_dir() / f"{name}.py"
    if f.is_file():
        return "file", f
    ep = _entry_points().get(name)
    if ep is None:
        raise PluginError(f"plugin {name!r} not found (bundled, {plugin_dir()}, or entry point group {GROUP})")
    return "package", ep


def code_file(kind: str, where):
    """The file whose SHA-256 is pinned for a non-bundled plugin."""
    from pathlib import Path
    if kind == "file":
        return where
    import importlib.util
    spec = importlib.util.find_spec(where.module)
    if not spec or not spec.origin:
        raise PluginError(f"cannot locate the code of {where.value}")
    return Path(spec.origin)


def pin(name: str) -> str:
    """Record the SHA-256 of a user/package plugin (done by `mg plugins enable`)."""
    kind, where = locate(name)
    if kind == "bundled":
        return ""
    f = code_file(kind, where)
    if kind == "file":
        check_owner(plugin_dir())
        check_owner(f)
    pins = read_pins()
    pins[name] = _sha256(f)
    import os
    p = pins_path()
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):  # also when the file existed
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(pins, fh, indent=1)
    return pins[name]


def _import(name: str):
    """Import an enabled plugin. Non-bundled code must match the SHA-256 pinned by `mg plugins enable`."""
    import importlib
    kind, where = locate(name)
    if kind == "bundled":
        return importlib.import_module(where), "bundled"
    f = code_file(kind, where)
    if kind == "file":
        check_owner(plugin_dir())
        check_owner(f)
    want = read_pins().get(name)
    if not want:
        raise PluginError(f"plugin {name!r} is not pinned; run: mg plugins enable {name}")
    import hashlib
    code = f.read_bytes()  # hash and execute the same bytes (no swap in between)
    if hashlib.sha256(code).hexdigest() != want:
        raise PluginError(f"plugin {name!r} changed since it was enabled ({f}); review it, then run "
                          f"mg plugins enable {name} again")
    if kind == "file":
        import types
        mod = types.ModuleType(f"mailgate_user_plugin_{name}")
        mod.__file__ = str(f)
        exec(compile(code, str(f), "exec"), mod.__dict__)  # nosec B102: pinned, user-enabled plugin code
        return mod, str(f)
    return where.load(), f"package {where.value}"


def available() -> list[tuple[str, str]]:
    """(name, source) of every plugin that could be enabled."""
    out = {n: "bundled" for n in _bundled()}
    if plugin_dir().is_dir():
        out.update({f.stem: str(f) for f in sorted(plugin_dir().glob("*.py")) if f.stem not in out})
    out.update({n: f"package {ep.value}" for n, ep in _entry_points().items() if n not in out})
    return sorted(out.items())


def load(cfg, db, publish: Callable[[dict], None] | None = None, names: list[str] | None = None,
         quiet: bool = False) -> Registry:
    """Load and set up the enabled plugins. Broken plugins are reported and skipped."""
    reg = Registry(cfg, db, publish)
    for name in (cfg.plugins if names is None else names):
        try:
            mod, source = _import(name)
            ver = getattr(mod, "api_version", None)
            if not isinstance(ver, int) or ver > API_VERSION or ver < 1:
                raise PluginError(f"plugin {name!r} needs api_version {ver}, this mailgate has {API_VERSION}")
            api = PluginAPI(name, reg, cfg.plugin_settings.get(name, {}))
            reg.apis[name] = api
            reg.loaded[name] = Loaded(name, source, (mod.__doc__ or "").strip().split("\n")[0])
            if hasattr(mod, "setup"):
                token = _active.set(name)
                try:
                    mod.setup(api)
                finally:
                    _active.reset(token)
        except Exception as e:
            reg.loaded[name] = Loaded(name, "?", error=f"{type(e).__name__}: {e}")
            if not quiet:
                _log(f"plugin {name}: not loaded: {type(e).__name__}: {e}")
    return reg


def label(text, lang: str = "en") -> str:
    return text.get(lang) or text.get("en") or next(iter(text.values()), "") if isinstance(text, dict) else str(text)
