# Changelog

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
