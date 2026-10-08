"""Config file, XDG paths and password lookup."""
from __future__ import annotations

import os
import re
import secrets
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
DEFAULT_PORTS = {("imap", "ssl"): 993, ("imap", "starttls"): 143, ("imap", "plain"): 143,
                 ("smtp", "ssl"): 465, ("smtp", "starttls"): 587, ("smtp", "plain"): 25}


class ConfigError(Exception):
    """Invalid or missing configuration."""


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def config_path() -> Path:
    """Config file location (MAILGATE_CONFIG overrides)."""
    if p := os.environ.get("MAILGATE_CONFIG"):
        return Path(p)
    return _xdg("XDG_CONFIG_HOME", ".config") / "mailgate" / "config.toml"


def db_path() -> Path:
    """SQLite cache location (MAILGATE_DB overrides)."""
    if p := os.environ.get("MAILGATE_DB"):
        return Path(p)
    return _xdg("XDG_DATA_HOME", ".local/share") / "mailgate" / "mail.db"


@dataclass
class Server:
    host: str
    port: int
    security: str  # ssl | starttls | plain (plain only on loopback)


def _secret(cmd: str | None, env: str | None, what: str) -> str:
    if env:
        val = os.environ.get(env)
        if val is None:
            raise ConfigError(f"{what}: environment variable {env} is not set")
        return val
    if cmd:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise ConfigError(f"{what}: command failed (exit {r.returncode})")
        return r.stdout.rstrip("\r\n")
    raise ConfigError(f"{what}: set password_cmd or password_env")


@dataclass
class Account:
    name: str
    email: str
    display_name: str
    user: str
    imap: Server
    smtp: Server
    password_cmd: str | None = None
    password_env: str | None = None
    folders: list[str] = field(default_factory=lambda: ["INBOX"])
    sent_folder: str | None = "Sent"
    signature: str = ""
    reply_prefix: str = "Re:"
    approval_mode: str | None = None  # per-account override of approval.mode
    archive_folder: str | None = None  # None: \Archive special-use folder, else "Archive"
    trash_folder: str | None = None  # None: \Trash special-use folder, else "Trash"

    def password(self) -> str:
        """Run password_cmd or read password_env. Never logged."""
        return _secret(self.password_cmd, self.password_env, f"account {self.name}")


@dataclass
class Ntfy:
    server: str
    topic: str
    reply_topic: str
    approve_label: str = "Send"
    reject_label: str = "Discard"
    stop_label: str = "Stop"
    priority: int = 4
    token_cmd: str | None = None
    token_env: str | None = None

    def auth_header(self) -> dict[str, str]:
        """Authorization header for a protected ntfy server, or {}."""
        if not (self.token_cmd or self.token_env):
            return {}
        return {"Authorization": "Bearer " + _secret(self.token_cmd, self.token_env, "ntfy token")}


MODES = ("manual", "auto", "rules")


@dataclass
class Rules:
    allow_to: list[str] = field(default_factory=list)  # fnmatch patterns, all recipients must match
    allow_accounts: list[str] | None = None
    reply_only: bool = False
    deny_attachments: bool = True


RULE_MATCH = ("from", "to", "subject", "list_id")


@dataclass
class SortRule:
    """[[rules]] entry: all given regexes must match (case-insensitive), then the actions run."""
    name: str
    match: dict[str, re.Pattern]  # from/to/subject/list_id -> regex
    headers: dict[str, re.Pattern]  # header name -> regex
    actions: list[str]  # move:<folder> | mark_read | flag
    account: str | None = None
    folder: str = "INBOX"


@dataclass
class UI:
    """[ui] section for `mg ui`."""
    port: int = 8766
    sync_minutes: float = 2.0
    idle: bool = True
    lang: str = "auto"  # auto | de | en
    mark_read: bool = True  # mark a message read when it is opened


@dataclass
class Config:
    accounts: dict[str, Account]
    default_account: str
    expiry_hours: float = 48.0
    ntfy: Ntfy | None = None
    max_raw_bytes: int = 5_000_000
    initial_days: int = 0
    mode: str = "manual"
    undo_seconds: int = 0
    max_per_hour: int = 20
    rules: Rules = field(default_factory=Rules)
    sort_rules: list[SortRule] = field(default_factory=list)
    ui: UI = field(default_factory=UI)

    def mode_for(self, acct: Account) -> str:
        """Effective approval mode for an account."""
        return acct.approval_mode or self.mode

    def account(self, name: str | None = None) -> Account:
        name = name or self.default_account
        if name not in self.accounts:
            raise ConfigError(f"unknown account '{name}' (have: {', '.join(self.accounts)})")
        return self.accounts[name]


def _server(t: dict, kind: str, acct: str) -> Server:
    host = t.get(f"{kind}_host")
    if not host:
        raise ConfigError(f"account {acct}: {kind}_host missing")
    sec = t.get(f"{kind}_security", "ssl")
    if sec not in ("ssl", "starttls", "plain"):
        raise ConfigError(f"account {acct}: {kind}_security must be ssl, starttls or plain")
    if sec == "plain" and host not in LOOPBACK:
        raise ConfigError(f"account {acct}: plain (unencrypted) {kind} is only allowed on localhost")
    return Server(host, int(t.get(f"{kind}_port", DEFAULT_PORTS[(kind, sec)])), sec)


def _mode(v: str | None, where: str) -> str | None:
    if v is not None and v not in MODES:
        raise ConfigError(f"{where}: approval mode must be one of {', '.join(MODES)}")
    return v


def _rx(v: str, where: str) -> re.Pattern:
    try:
        return re.compile(v, re.I)
    except re.error as e:
        raise ConfigError(f"{where}: bad regex: {e}") from None


def _sort_rules(items: list) -> list[SortRule]:
    out = []
    for i, t in enumerate(items or [], 1):
        name = str(t.get("name") or f"rule {i}")
        acts = t.get("action") or t.get("actions") or []
        acts = [acts] if isinstance(acts, str) else list(acts)
        for a in acts:
            if a not in ("mark_read", "flag") and not (a.startswith("move:") and a[5:].strip()):
                raise ConfigError(f"rules '{name}': action must be move:<folder>, mark_read or flag, not {a!r}")
        match = {k: _rx(t[k], f"rules '{name}'") for k in RULE_MATCH if t.get(k)}
        headers = {k: _rx(v, f"rules '{name}'") for k, v in (t.get("header") or {}).items()}
        if not (match or headers) or not acts:
            raise ConfigError(f"rules '{name}': needs at least one match and one action")
        out.append(SortRule(name, match, headers, acts, t.get("account"), t.get("folder", "INBOX")))
    return out


def parse(data: dict) -> Config:
    """Build a Config from parsed TOML."""
    accts: dict[str, Account] = {}
    for name, t in (data.get("accounts") or {}).items():
        if "password" in t:
            raise ConfigError(f"account {name}: plaintext 'password' is not supported, "
                              "use password_cmd or password_env")
        if not (t.get("password_cmd") or t.get("password_env")):
            raise ConfigError(f"account {name}: password_cmd or password_env required")
        email = t.get("email") or ""
        if "@" not in email:
            raise ConfigError(f"account {name}: email missing")
        accts[name] = Account(
            name=name, email=email, display_name=t.get("name", ""), user=t.get("user", email),
            imap=_server(t, "imap", name), smtp=_server(t, "smtp", name),
            password_cmd=t.get("password_cmd"), password_env=t.get("password_env"),
            folders=list(t.get("folders", ["INBOX"])), sent_folder=t.get("sent_folder", "Sent") or None,
            signature=t.get("signature", ""), reply_prefix=t.get("reply_prefix", "Re:"),
            approval_mode=_mode(t.get("approval_mode"), f"account {name}"),
            archive_folder=t.get("archive_folder"), trash_folder=t.get("trash_folder"))
    if not accts:
        raise ConfigError("no [accounts.NAME] section in config")
    ap = data.get("approval") or {}
    ntfy = None
    if (n := ap.get("ntfy")) and n.get("enabled", True):
        if not (n.get("topic") and n.get("reply_topic")):
            raise ConfigError("approval.ntfy: topic and reply_topic required")
        if n["topic"] == n["reply_topic"]:
            raise ConfigError("approval.ntfy: reply_topic must differ from topic")
        ntfy = Ntfy(server=n.get("server", "https://ntfy.sh").rstrip("/"), topic=n["topic"],
                    reply_topic=n["reply_topic"], approve_label=n.get("approve_label", "Send"),
                    reject_label=n.get("reject_label", "Discard"), stop_label=n.get("stop_label", "Stop"),
                    priority=int(n.get("priority", 4)),
                    token_cmd=n.get("token_cmd"), token_env=n.get("token_env"))
    r = ap.get("rules") or {}
    rules = Rules(allow_to=[p.lower() for p in r.get("allow_to", [])],
                  allow_accounts=list(r["allow_accounts"]) if "allow_accounts" in r else None,
                  reply_only=bool(r.get("reply_only", False)), deny_attachments=bool(r.get("deny_attachments", True)))
    sync = data.get("sync") or {}
    default = data.get("default_account") or next(iter(accts))
    if default not in accts:
        raise ConfigError(f"default_account '{default}' is not defined")
    return Config(accounts=accts, default_account=default,
                  expiry_hours=float(ap.get("expiry_hours", 48)), ntfy=ntfy,
                  max_raw_bytes=int(sync.get("max_raw_bytes", 5_000_000)),
                  initial_days=int(sync.get("initial_days", 0)),
                  mode=_mode(ap.get("mode", "manual"), "approval") or "manual",
                  undo_seconds=max(0, int(ap.get("undo_seconds", 0))),
                  max_per_hour=max(0, int(ap.get("max_per_hour", 20))), rules=rules,
                  sort_rules=_sort_rules(data.get("rules")), ui=_ui(data.get("ui") or {}))


def _ui(t: dict) -> UI:
    lang = t.get("lang", "auto")
    if lang not in ("auto", "de", "en"):
        raise ConfigError("ui.lang must be auto, de or en")
    return UI(port=int(t.get("port", 8766)), sync_minutes=max(0.25, float(t.get("sync_minutes", 2))),
              idle=bool(t.get("idle", True)), lang=lang, mark_read=bool(t.get("mark_read", True)))


def load(path: Path | None = None) -> Config:
    """Load and validate the config file."""
    path = path or config_path()
    if not path.exists():
        raise ConfigError(f"no config at {path}, run: mg init")
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return parse(data)


EXAMPLE = '''# mailgate config. Docs: https://github.com/DennisKossert/mailgate
# Passwords are never stored here. Use password_cmd (stdout is the password)
# or password_env (name of an environment variable).

# default_account = "work"

[accounts.work]
email = "jane@example.com"
name = "Jane Doe"                 # display name in From:
# user = "jane@example.com"       # login name, defaults to email
imap_host = "imap.example.com"
# imap_port = 993
imap_security = "ssl"             # ssl (993) or starttls (143)
smtp_host = "smtp.example.com"
# smtp_port = 465
smtp_security = "ssl"             # ssl (465) or starttls (587)
password_cmd = "secret-tool lookup service mailgate account work"
# password_env = "MAILGATE_WORK_PASSWORD"
folders = ["INBOX"]               # folders to sync
sent_folder = "Sent"              # copy of sent mail is appended here ("" to disable)
signature = """Jane Doe
Example Ltd."""
# reply_prefix = "AW:"            # used when the subject has no Re:/AW: yet
# approval_mode = "manual"        # per-account override of approval.mode
# archive_folder = "Archive"      # used by `mg ui` / `mg move --archive` (default: \\Archive folder)
# trash_folder = "Trash"          # delete in the web UI moves here (default: \\Trash folder)

[sync]
max_raw_bytes = 5000000           # raw messages larger than this are not cached
initial_days = 90                 # first sync only fetches the last N days (0 = all)

[approval]
expiry_hours = 48                 # pending drafts expire after this
mode = "manual"                   # manual | auto | rules. auto/rules send WITHOUT a human
                                  # looking at the mail. Read the README first.
# undo_seconds = 60               # auto/rules: wait this long before sending (needs mg daemon)
# max_per_hour = 20               # unattended sends per account per hour, then manual

# [approval.rules]                # only used with mode = "rules"
# allow_to = ["*@example.com"]    # every recipient (To, Cc, Bcc) must match a pattern
# allow_accounts = ["work"]
# reply_only = true               # only replies to existing mail (mg reply)
# deny_attachments = true

[approval.ntfy]
# Topic names are secrets on public servers: anyone who knows them can read them.
server = "https://ntfy.sh"
topic = "{topic}"
reply_topic = "{reply}"
approve_label = "Send"            # e.g. "Senden"
reject_label = "Discard"          # e.g. "Verwerfen"
# token_cmd = "secret-tool lookup service ntfy"   # access token for a protected server

# [ui]                            # optional web client for humans: mg ui
# port = 8766
# sync_minutes = 2                # background sync while mg ui runs (plus IMAP IDLE on INBOX)
# lang = "auto"                   # auto (browser language) | de | en
# mark_read = true                # mark mail read when opened

# Sorting rules, applied by `mg ui` (or `mg rules apply`) to new mail after a sync.
# Regexes are case-insensitive. Check them first with: mg rules test
# [[rules]]
# name = "Newsletters"
# list_id = 'news\\.example\\.com'   # also: from, to, subject (literal strings, no escaping)
# action = ["mark_read", "move:Newsletter"]
#
# [[rules]]
# name = "Invoices"
# from = 'billing@example\\.net'
# header = { "X-Priority" = "^1" }
# action = "flag"
'''


def example() -> str:
    """Example config with fresh random ntfy topic names."""
    return EXAMPLE.replace("{topic}", "mg-" + secrets.token_urlsafe(12)).replace(
        "{reply}", "mg-" + secrets.token_urlsafe(12))
