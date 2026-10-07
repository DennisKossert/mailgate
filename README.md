# mailgate

A small mail CLI for AI agents. It reads your mail through IMAP into a local SQLite
cache and prints it in a form that costs few tokens. Agents can write drafts, but
nothing is sent until a human approves it, on the phone through
[ntfy](https://ntfy.sh), in a local web page, or in a terminal.

Python 3.11+, standard library only, MIT license.

## Why

- **Agents and a human in the loop.** Letting an agent read your inbox is useful.
  Letting it send mail on its own is a risk: a misunderstanding or a prompt injection
  hidden in an incoming mail can make it write to the wrong people. With mailgate the
  agent prepares the mail and you press Send.
- **Token efficiency.** One line per mail in lists. Message bodies come without quoted
  replies, signatures, legal footers, HTML and tracking links, so a long reply chain shrinks
  to the new text.
- **Your own servers.** It talks plain IMAP and SMTP to whatever provider you use.
  No cloud service, no account with anyone, no telemetry. ntfy is optional and can be
  self-hosted.

## What it looks like

```
$ mg ls -n 3
k3f*@ 10-05 14:02 work/INBOX Example Billing | Invoice 2026-10
k3e 10-05 09:40 work/INBOX Max Mustermann | Re: Thursday meeting
k3d* 10-04 18:12 home/INBOX Jane Doe | Photos from Sunday

$ mg read k3e
From: Max Mustermann <max@example.org>
To: Jane Doe <jane@example.com>
Date: 2026-10-05 09:40
Subject: Re: Thursday meeting

Thursday at 10 works for me. I will bring the printed plans.

Max

$ mg reply k3e --body-file answer.txt
d7 queued, waiting for human approval (expires 10-07 09:45)
From: Jane Doe <jane@example.com>
To: Max Mustermann <max@example.org>
Subject: Re: Thursday meeting

Great, see you then.
```

The ids are base36 row ids. `*` marks unread mail, `@` mail with attachments.

## Install

```
pipx install git+https://github.com/DennisKossert/mailgate
```

or from a checkout: `pip install --user .` (or `python -m mailgate` without installing).

## Quick start

```
mg init            # writes ~/.config/mailgate/config.toml (mode 600)
$EDITOR ~/.config/mailgate/config.toml
mg doctor          # checks IMAP, SMTP and ntfy without printing secrets
mg sync
mg ls
```

Passwords are never stored in the config. Use `password_cmd` (any command that prints
the password, for example `secret-tool lookup service mailgate account work` or
`pass show mail/work`) or `password_env` (name of an environment variable).

The cache lives in `~/.local/share/mailgate/mail.db` (respects `XDG_DATA_HOME`,
override with `MAILGATE_DB`). The config path can be overridden with `MAILGATE_CONFIG`.

## Commands

| Command | Purpose |
|---|---|
| `mg sync [--acct A] [--folders F...]` | Fetch new mail. Incremental by UIDVALIDITY and UID, read-only on the server (EXAMINE, BODY.PEEK). |
| `mg ls [-n 20] [--unread] [--since 7d] [--from X] [--acct A] [--folder F] [--json]` | One line per mail, newest first. |
| `mg search WORDS... [filters]` | Full-text search over sender, subject and body (SQLite FTS5, LIKE fallback). |
| `mg read ID [--max 2000] [--full] [--json]` | Cleaned text with a short header block and attachment list. |
| `mg thread ID [--full]` | The whole conversation, oldest first, each message quote-stripped. |
| `mg att ID [--out DIR]` | Save attachments. |
| `mg stats` | Counts per account and folder. |
| `mg new WATCHER [--match REGEX] [--backlog]` | Mail this watcher has not seen yet, then marks it seen. For notifiers. |
| `mg draft --to X [--cc Y] [--bcc Z] --subject S --body-file F [--attach FILE]` | Queue a new mail. |
| `mg reply ID --body-file F [--all]` | Queue a reply with In-Reply-To/References. Keeps an existing `Re:`/`AW:`. |
| `mg queue [--full]`, `mg cancel ID` | Show or withdraw pending drafts. |
| `mg approve ID` | Send a draft from an interactive terminal (you retype the id). |
| `mg log` | Audit log: queued, sent, rejected, expired, failed, bad tokens. |
| `mg daemon [--web 127.0.0.1:8765] [--no-ntfy]` | Approval listener, sends scheduled drafts, optional web UI. |
| `mg doctor` (`mg config-check`) | Check config, approval mode and connectivity. |

The first run of a new watcher only remembers what is already there and prints nothing,
so a notifier does not flood you. Use `--backlog` to print existing mail once.

## Approval

Every draft is rendered to its final MIME bytes when it is created. The SHA-256 of these
bytes is stored, and before sending mailgate checks that the bytes are unchanged. What
you approved is exactly what goes out over SMTP and what is appended to the Sent folder
(flagged `\Seen`). Drafts expire after 48 hours by default (`approval.expiry_hours`).

There are three ways to approve. They can be combined.

### ntfy (phone)

When a draft is created, mailgate publishes a notification to your ntfy topic with the
exact From, To, Cc, Subject and body (the body is cut at about 3,500 bytes with a note).
It has two buttons. They POST `approve <draft> <token>` or `reject <draft> <token>` to a
second topic, the reply topic. `mg daemon` listens on the reply topic, checks the token
with a constant-time compare and then sends or discards the draft. The token is a random
per-draft secret; only its SHA-256 is stored.

1. Install the ntfy app and subscribe to the topic from your config.
   `mg init` creates random topic names. On the public ntfy.sh server the topic name is
   the only protection, so keep it secret. A self-hosted ntfy server with access control
   is better: set `token_cmd` or `token_env` for the access token.
2. Set the button labels if you like: `approve_label = "Senden"`, `reject_label = "Verwerfen"`.
3. Run `mg daemon` permanently, for example as a systemd user service:

```ini
# ~/.config/systemd/user/mailgate.service
[Unit]
Description=mailgate approval daemon
After=network-online.target

[Service]
ExecStart=%h/.local/bin/mg daemon --web 127.0.0.1:8765
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```
systemctl --user daemon-reload
systemctl --user enable --now mailgate.service
journalctl --user -u mailgate -f
```

The daemon remembers the last ntfy message id, so approvals you press while it is
restarting are picked up as long as the ntfy server still has them (12 hours on ntfy.sh).

A sync timer works the same way: a `mailgate-sync.service` with `ExecStart=%h/.local/bin/mg sync`
and a `mailgate-sync.timer` with `OnCalendar=*:0/5`.

### Local web UI

`mg daemon --web 127.0.0.1:8765` serves a page with the pending drafts and Send / Discard
buttons. It only binds to loopback addresses, checks the Host and Origin headers, and
uses a CSRF token that changes on every daemon start.

### Terminal

`mg approve d7` shows the full draft and asks you to type the draft id again. It refuses
to run when stdin or stdout is not a terminal.

## Sending without approval (opt-in)

The default is `mode = "manual"`: nothing is sent without a human. Some setups want
an agent to send on its own, for example status replies to colleagues. This is possible,
but only if you turn it on.

```toml
[approval]
mode = "rules"          # manual (default) | auto | rules
undo_seconds = 60       # wait before sending; you can stop it meanwhile
max_per_hour = 20       # unattended sends per account per hour, then manual again

[approval.rules]        # only used with mode = "rules"
allow_to = ["*@example.com", "boss@example.org"]   # every To/Cc/Bcc must match
allow_accounts = ["work"]
reply_only = true       # only replies to existing mail (mg reply)
deny_attachments = true

[accounts.private]
approval_mode = "manual"   # per-account override
```

- **manual**: every draft waits for approval, as described above.
- **auto**: every draft is sent without anyone looking at it.
- **rules**: a draft that passes all rules is sent automatically. Anything else falls back
  to normal approval, with the ntfy Send/Discard buttons, and `mg draft` prints which
  rule failed.

With `undo_seconds = 0` the draft is sent right away by `mg draft` and it prints `sent`.
With an undo window it prints `queued, auto-send in 60s` and **`mg daemon` sends it** once
the window is over. Without a running daemon a scheduled draft is never sent. During the
window you can stop it with the ntfy "Stop" button (`stop_label`), the web UI or
`mg cancel ID`.

When the hourly limit is reached, drafts fall back to manual approval and you get the
normal notification. Every unattended send is in `mg log` with `auto` or `auto-rules` and
the mode, and ntfy (if configured) gets a "sent automatically" message. `mg doctor`
(alias `mg config-check`) prints a `WARN` line for every account in auto mode.

Be clear about what this means: in auto mode, an agent mistake or a prompt injection in
an incoming mail ("forward the last invoices to ...") can make mailgate send mail in
your name. If you need unattended sending, use `rules` with a narrow `allow_to`,
`reply_only = true`, `deny_attachments = true` and an undo window, and keep `max_per_hour` low.

## Security model

Read this before you rely on it.

- Everything below assumes `mode = "manual"`. With auto or rules mode, the protection is
  only as good as your rules (see above).
- The approval queue protects against **mistakes and prompt injection**: an agent that
  misread a request, or a mail that tells the agent to forward your data somewhere. The
  agent can only create drafts, and you see the exact text before it leaves.
- It does **not** protect against a compromised machine. mailgate runs as your user.
  Any process running as your user can read the config, run your `password_cmd`, open the
  local web UI, fake a TTY, or simply talk SMTP itself. If an agent has unrestricted shell
  access, it could do the same. Use the agent's own permission settings to keep it to `mg`
  commands, and treat mailgate as a seatbelt, not a vault.
- Whoever can read your ntfy notification topic can see the approval tokens. Use secret
  topic names or a self-hosted server with access control.
- The cache contains your mail in plain form. It is protected by file permissions only.
  Use disk encryption.
- `mg doctor` and the logs never print passwords or tokens.

## Comparison

Typical MCP mail servers give the model a `send_email` tool that sends directly, and at
most rely on the client asking to confirm the tool call. In its default mode mailgate has no send path for the agent at all: drafting and sending
are separate steps, and sending needs a human action outside the agent session.
Unattended sending exists only as an explicit opt-in with rules and a rate limit.

## Configuration

See the commented file written by `mg init`. In short:

```toml
[accounts.work]
email = "jane@example.com"
name = "Jane Doe"
imap_host = "imap.example.com"   # imap_security = "ssl" (993) or "starttls" (143)
smtp_host = "smtp.example.com"   # smtp_security = "ssl" (465) or "starttls" (587)
password_cmd = "secret-tool lookup service mailgate account work"
folders = ["INBOX"]
sent_folder = "Sent"
signature = "Jane Doe"

[sync]
max_raw_bytes = 5000000   # bigger messages are not cached raw (att fetches them on demand)
initial_days = 90         # first sync only fetches recent mail

[approval]
expiry_hours = 48

[approval.ntfy]
server = "https://ntfy.sh"
topic = "mg-random-secret-1"
reply_topic = "mg-random-secret-2"
```

Unencrypted IMAP/SMTP (`plain`) is only accepted for `127.0.0.1`/`localhost`, for example
for a local bridge or tests.

## For agents

`AGENTS.md` is a short cheat sheet for agents. `docs/claude-code.md` has a snippet for
`CLAUDE.md`.

## Development

```
python -m unittest
```

The tests start small fake IMAP, SMTP and ntfy servers on localhost. No network access
and no real mail account are needed.

## Roadmap

Not built yet:

- MCP server wrapper exposing the read commands and `draft`/`reply` (still no send).
- OAuth2 (XOAUTH2) for Gmail and Outlook.
- Sieve rule management.

## License

MIT, copyright Dennis Kossert / KOSSERT.

## Deutsch

mailgate ist ein kleines Mail-Programm für die Kommandozeile, gebaut für KI-Agenten. Es
holt Mails per IMAP in einen lokalen SQLite-Cache und gibt sie so knapp aus, dass ein
Sprachmodell wenig Tokens dafür braucht: eine Zeile pro Mail, Texte ohne Zitate, Signaturen,
Disclaimer und Tracking-Links.

Agenten können Entwürfe anlegen, aber im Standardmodus nichts selbst versenden. Jeder Entwurf landet in einer
Warteschlange und geht erst raus, wenn ein Mensch zustimmt: per ntfy-Benachrichtigung aufs
Handy (Knöpfe zum Beispiel "Senden" und "Verwerfen"), über eine lokale Webseite oder im
Terminal mit `mg approve`. Verschickt wird genau der Text, den man freigegeben hat; er wird
beim Anlegen gehasht und vor dem Senden geprüft.

Wer möchte, kann automatisches Senden ausdrücklich einschalten (`mode = "auto"` oder
`mode = "rules"` mit erlaubten Empfängern, nur Antworten, ohne Anhänge, Wartezeit zum
Abbrechen und Stundenlimit). Dann kann aber ein Fehler des Agenten oder eine manipulierte
eingehende Mail dazu führen, dass in Ihrem Namen gesendet wird. Empfohlen ist `rules`
mit engem `allow_to`, `reply_only = true` und `undo_seconds`.

Installation: `pipx install git+https://github.com/DennisKossert/mailgate`, danach `mg init`,
Konfiguration anpassen, `mg doctor`, `mg sync`. Passwörter stehen nie in der Konfiguration,
sondern kommen aus einem Befehl (`password_cmd`) oder einer Umgebungsvariable (`password_env`).

Zur Sicherheit: Die Warteschlange schützt vor Fehlern des Agenten und vor Prompt Injection
in eingehenden Mails. Sie schützt nicht vor einem kompromittierten Rechner, denn jeder
Prozess unter dem eigenen Benutzer könnte eine Freigabe vortäuschen oder direkt per SMTP
senden. ntfy-Topics auf ntfy.sh sind nur durch ihren Namen geschützt; am besten zufällige
Namen verwenden oder einen eigenen ntfy-Server mit Zugriffsschutz.
