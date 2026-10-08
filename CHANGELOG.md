# Changelog

## 0.4.0 (2026-10-08)

Small core, plugins for the rest, and a security pass.

- Plugin system (`api_version = 1`, stdlib only): bundled plugins, single files in the config
  folder's `plugins/`, and pip packages (entry point group `mailgate.plugins`). Enabled per
  config (`[plugins] enabled`, settings in `[plugins.<name>]`); `mg plugins list|info|enable|disable`.
  Hooks: message synced (metadata), new mail, render (badges/banners/`mg read` lines), link filter,
  list filter, scheduled ticks, rule conditions and actions, CLI commands, UI message actions and
  sidebar views (as JSON, no script injection). See PLUGINS.md.
- Security invariant in the core: plugins, rules and run hooks can only create drafts. Every send
  path refuses while plugin code runs; plugin drafts always wait for a human, even in auto mode.
- Non-bundled plugins load only after `mg plugins enable` (interactive), which pins their SHA-256;
  changed, foreign-owned or group/world-writable plugin files are refused. Plugin errors are
  isolated and logged.
- Bundled plugins: `auth` (SPF/DKIM/DMARC badge, look-alike sender and misleading-link warnings,
  `Trust:` line in `mg read`), `linkclean` (tracking parameters), `dedupe` (same Message-ID once in
  unified views) on by default; `unsubscribe` (RFC 8058 one-click, https only and only on click,
  mailto as approval-bound draft, Newsletters view, `mg unsub`), `attachments`
  (`save_attachments:<dir>` with safe names and SHA-256 dedupe), `followup` (`mg remind`, `mg snooze`,
  Follow-ups view, ntfy when due) opt-in.
- `mg daemon --sync`: headless background sync with IMAP IDLE, sorting rules and plugin ticks.
- Rules: core actions `archive`, `trash`, `run:<command>` (no shell, JSON on stdin, reduced
  environment, timeout); plugin conditions and actions.
- `mg events [-f]`: local event stream as JSON lines.
- `mg export` / `mg import`: config, rules, signatures, watcher state, pending drafts, plugin data;
  `--with-secrets` encrypts with AES-256-GCM and a one-time code (optional `cryptography`, refuses
  otherwise); `--pair` / "Transfer to another device" serves it once over the LAN with QR code
  (vendored Nayuki QR encoder, MIT). Imported passwords go to the system keyring.
- Config and data folders per OS (XDG, macOS Application Support, Windows %APPDATA%); files created
  0600, folders 0700, with warnings in `mg doctor`, `mg ui` and `mg daemon`.
- Fixes from the security review: attribute names in mail HTML are validated, NUL bytes stripped;
  ntfy server must be http(s); export/config files get mode 600 even when they existed; JSON body
  type checked without `assert`.
- SECURITY.md, PLUGINS.md, examples/hello_plugin.py.

## 0.3.0 (2026-10-08)

Optional web mail client for humans.

- `mg ui`: local single-page web mail client (stdlib server, plain HTML/CSS/JS, works offline).
  Three panes on desktop, one pane on phones, unified inbox, per-account folders, search,
  infinite scroll, keyboard shortcuts, light/dark mode, German and English.
- Safe HTML display: rewritten markup, strict Content-Security-Policy, sandboxed iframe,
  remote images only on request, inline cid images from the cache, cleaned "Text" view,
  attachment downloads, conversation view.
- Compose, reply, reply all and forward with attachments. Sending from the UI is a human
  action and skips the approval queue; it is still audited (`via ui`) and appended to Sent.
- UI passphrase (scrypt hash, 0600), session cookie (HttpOnly, SameSite=Strict, 12 h idle),
  CSRF token on every change. Without a passphrase the UI is read-only.
- "Approvals" view with Send / Discard for drafts queued by agents.
- Background sync while `mg ui` runs, IMAP IDLE on INBOX, live updates via Server-Sent Events,
  opt-in desktop notifications.
- New `imapops` module: flags, move (UID MOVE, or COPY + UID EXPUNGE of only that UID),
  archive, move to Trash (never permanent deletion from the UI). New CLI commands
  `mg mark` and `mg move`. `mg sync` stays read-only.
- Sorting rules (`[[rules]]`: from/to/subject/list_id/header regexes, actions move/mark_read/flag),
  applied to new mail by `mg ui`; `mg rules test` (dry run) and `mg rules apply`.
- Per-account `archive_folder` / `trash_folder`, `[ui]` config section, example in `mg init`.
- Replying to your own sent mail now addresses its recipients instead of yourself.
- `python -m tests.demo_ui`: demo with fake servers and example.com mail.

## 0.2.0 (2026-10-07)

- Opt-in unattended sending: `approval.mode = manual | auto | rules`, per-account override,
  undo window with ntfy Stop button, hourly rate limit with fallback to manual approval.

## 0.1.0 (2026-10-07)

- First version: IMAP sync into SQLite, token-efficient `ls`/`read`/`thread`/`search`,
  drafts and replies with human approval via ntfy, local web page or terminal.
