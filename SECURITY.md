# Security

## Reporting

Please report security problems privately through GitHub's "Report a vulnerability"
(Security tab of DennisKossert/mailgate), not in a public issue. Include the version
(`mg --version`), what you did and what happened. You will get an answer within a week.

## Threat model

mailgate lets AI agents read mail and prepare replies while a human decides what is sent.

Protected against:

- **Agent mistakes and prompt injection.** Agents can only create drafts. Sending needs the
  approval token (ntfy button), an interactive terminal (`mg approve`), the passphrase-protected
  web UI session, or an explicit opt-in to unattended sending (`approval.mode = "auto"` or
  `"rules"`, with recipient rules and an hourly limit).
- **Plugins, rules and run hooks sending mail.** They can create drafts only. Every send path
  checks for running plugin code and refuses; plugin drafts always wait for a human.
- **Hostile mail content.** HTML is rewritten (no scripts, event handlers, forms, frames,
  `<base>`, `<meta>`), served with `Content-Security-Policy: default-src 'none'` and shown in a
  sandboxed iframe without scripts. Remote images are off until you allow them per message.
  Attachment names are reduced to safe characters and never leave the target folder; nothing
  is executed. Unsubscribing contacts only https URLs, and only after a human click.
  Mail data never goes into a command line.
- **Terminal escape injection.** Subjects, names and bodies are stripped of escape sequences,
  control characters and bidi overrides before any `mg` command or `mg tui` prints them.
- **Macros and plugins in `mg tui`.** Sending from the TUI needs the draft id typed at the
  keyboard; macro replay, `on_start`, plugin commands and key mappings run from them are refused.
- **Other local users.** Config, cache, passphrase hash, plugin pins and exports are created
  with mode 600, folders with 700; `mg doctor`, `mg ui` and `mg daemon` warn when they are not.
- **The local network.** The web UI listens on 127.0.0.1/::1 only. The only LAN listener is
  pairing (`mg export --pair` or the UI button), which serves one AES-256-GCM encrypted
  bundle, for 10 minutes, once, to a client that proves the 60-bit one-time code (the code
  itself never crosses the network); five wrong attempts lock it.

Not protected against:

- **A compromised account or machine.** Anything that runs as your user can read the config
  and cache, run your `password_cmd`, replace the passphrase hash or talk SMTP directly. The
  approval queue is a seatbelt, not a vault. Give agents limited shell rights.
- **Malicious plugins.** Plugins are trusted Python code (see PLUGINS.md). Pinning and
  ownership checks stop silent changes, not a plugin you chose to enable.
- **Readers of your ntfy topic.** On public ntfy servers the topic name is the secret. Use a
  self-hosted server with access control if that matters.
- **Unencrypted disks.** The cache holds your mail in plain form.

## Design rules for contributors

- No `shell=True` with anything derived from mail; `password_cmd` is the user's own command.
- Every new send path must call `plugin.assert_not_plugin()`.
- Every write endpoint of the UI needs the session and the CSRF token; no new listeners
  without an explicit flag or click.
- Secrets are never written unencrypted: `mg export --with-secrets` refuses without the
  `cryptography` package.
