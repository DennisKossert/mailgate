"""Config file, XDG paths and password lookup."""
from __future__ import annotations

import os
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
    priority: int = 4
    token_cmd: str | None = None
    token_env: str | None = None

    def auth_header(self) -> dict[str, str]:
        """Authorization header for a protected ntfy server, or {}."""
        if not (self.token_cmd or self.token_env):
            return {}
        return {"Authorization": "Bearer " + _secret(self.token_cmd, self.token_env, "ntfy token")}


@dataclass
class Config:
    accounts: dict[str, Account]
    default_account: str
    expiry_hours: float = 48.0
    ntfy: Ntfy | None = None
    max_raw_bytes: int = 5_000_000
    initial_days: int = 0

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
            signature=t.get("signature", ""), reply_prefix=t.get("reply_prefix", "Re:"))
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
                    reject_label=n.get("reject_label", "Discard"), priority=int(n.get("priority", 4)),
                    token_cmd=n.get("token_cmd"), token_env=n.get("token_env"))
    sync = data.get("sync") or {}
    default = data.get("default_account") or next(iter(accts))
    if default not in accts:
        raise ConfigError(f"default_account '{default}' is not defined")
    return Config(accounts=accts, default_account=default,
                  expiry_hours=float(ap.get("expiry_hours", 48)), ntfy=ntfy,
                  max_raw_bytes=int(sync.get("max_raw_bytes", 5_000_000)),
                  initial_days=int(sync.get("initial_days", 0)))


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

[sync]
max_raw_bytes = 5000000           # raw messages larger than this are not cached
initial_days = 90                 # first sync only fetches the last N days (0 = all)

[approval]
expiry_hours = 48                 # pending drafts expire after this

[approval.ntfy]
# Topic names are secrets on public servers: anyone who knows them can read them.
server = "https://ntfy.sh"
topic = "{topic}"
reply_topic = "{reply}"
approve_label = "Send"            # e.g. "Senden"
reject_label = "Discard"          # e.g. "Verwerfen"
# token_cmd = "secret-tool lookup service ntfy"   # access token for a protected server
'''


def example() -> str:
    """Example config with fresh random ntfy topic names."""
    return EXAMPLE.replace("{topic}", "mg-" + secrets.token_urlsafe(12)).replace(
        "{reply}", "mg-" + secrets.token_urlsafe(12))
