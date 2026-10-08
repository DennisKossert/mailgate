# mailgate plugins

mailgate keeps a small core (sync, cache and search, cleaned reading, compose, approval
queue, sending, the web UI shell, the rules engine). Everything else is a plugin. Plugins
are plain Python, standard library only, and use a small versioned API (`api_version = 1`).

## Using plugins

```
mg plugins list               # bundled, local and installed plugins, enabled or not
mg plugins info followup      # what it does
mg plugins enable followup    # adds it to [plugins] enabled in your config
mg plugins disable followup
```

Bundled plugins (reviewed with mailgate):

| Plugin | Default | What it does |
|---|---|---|
| `auth` | on | SPF/DKIM/DMARC badge, warnings for look-alike sender names and links whose text shows another domain; `Trust:` line in `mg read`; rule conditions `auth = "fail"`, `suspicious = true` |
| `linkclean` | on | strips tracking parameters (utm_*, fbclid, gclid, mc_eid, ...) from links in the UI |
| `dedupe` | on | shows a mail once in "All inboxes" and search when the same Message-ID is in several folders or accounts |
| `unsubscribe` | off | "Unsubscribe" button and a "Newsletters" view (List-Unsubscribe, RFC 8058 one-click), `mg unsub list/run` |
| `attachments` | off | rule action `save_attachments:<dir>` (e.g. invoices as PDF into a folder) |
| `followup` | off | follow-up reminders and snooze: `mg remind`, `mg snooze`, "Follow-ups" view, ntfy notification when due |

Settings live in `[plugins.<name>]`:

```toml
[plugins]
enabled = ["auth", "linkclean", "dedupe", "followup"]   # replaces the default list

[plugins.followup]
default = "3d"
```

## Writing a plugin in 20 lines

Save as `~/.config/mailgate/plugins/boss.py` (mode 600), then run `mg plugins enable boss`:

```python
"""Tags mail from the boss and adds `mg boss`."""
api_version = 1


def setup(mg):
    boss = mg.settings.get("address", "boss@example.com").lower()

    @mg.on_render
    def badge(msg):
        if msg.from_addr == boss:
            return {"badges": [{"text": "boss", "tone": "bad"}], "lines": ["Note: from your boss"]}

    @mg.command("boss", "list recent mail from the boss")
    def cmd(args):
        for m in mg.messages("from_addr=?", (boss,), limit=10):
            print(m.id, m.subject)
```

The same file is in `examples/hello_plugin.py`. To ship a plugin as a package, add an entry
point: `[project.entry-points."mailgate.plugins"] boss = "mailgate_boss"`. The user still has to
run `mg plugins enable boss`.

## Hook reference (api_version 1)

Registration (decorators on the `mg` object passed to `setup`):

| Hook | Called | Signature |
|---|---|---|
| `@mg.on_message_synced` | every new cached message, also on `mg sync`; **local only** (no server changes) | `fn(msg) -> dict \| None`, the dict is stored as this plugin's metadata (`msg.meta()`) |
| `@mg.on_new_mail` | after a background sync (`mg ui`, `mg daemon --sync`), after the rules | `fn(msg)` |
| `@mg.on_render` | when a message is shown (`mg read`, UI) | `fn(msg) -> {"badges": [{"text","tone","title"}], "banners": [{"text","tone"}], "lines": [str]}` |
| `@mg.link_filter` | for every link shown in the UI | `fn(href, text) -> (href, warning \| None)` |
| `@mg.list_filter` | message lists in the UI | `fn(items, view) -> items`; `view = {"kind": "unified"\|"folder"\|"search", "acct", "folder", "q"}` |
| `@mg.every(minutes)` | while `mg ui` or `mg daemon --sync` runs | `fn()` |
| `@mg.rule_condition("key")` | `key = value` in a `[[rules]]` entry | `fn(msg, value) -> bool` |
| `@mg.rule_action("kind", options=(...))` | `action = "kind:arg"` | `fn(msg, arg, rule) -> str`; `rule.extra` holds the option keys |
| `@mg.command("name", "help", args)` | `mg name ...` (cannot replace core commands) | `fn(args)`; `args(parser)` adds argparse options |
| `@mg.ui_action("id", label, choices=, when=, confirm=, icon=)` | button on an open message | `fn(msg, choice) -> str` |
| `@mg.ui_view("id", label, badge=, icon=)` | sidebar view | `fn(query) -> {"items": [{"key","title","sub","meta","msg"}], "multi": bool, "empty": label}` |
| `@mg.view_action("view_id", "id", label, choices=, confirm=)` | button in a view, works on the selected keys | `fn(keys, choice) -> str` |
| `@mg.tui_command("name", "help")` | `:name args` in `mg tui` | `fn(tui, args, msgs) -> str`; `tui.status/current/selection/run/confirm` |
| `mg.tui_keymap(mode, key, command)` | default key binding in `mg tui` (tui.toml wins) | plain call |
| `@mg.tui_statusline` | segment on the right of the `mg tui` status line | `fn(tui) -> str` |

Labels are a string or `{"en": ..., "de": ...}`. UI extensions are data (JSON) rendered by
the core page; plugins cannot inject scripts or markup into the page or the mail frame.

Methods:

| Method | |
|---|---|
| `mg.settings`, `mg.name`, `mg.accounts` | settings table, plugin name, `[{name, email, display_name}]` (no server data, no passwords) |
| `mg.q(sql, args)` | read-only SQL over the cache (`msgs`, `folders`, `drafts`, `audit`, ...) |
| `mg.message(id)`, `mg.messages(where, args, limit)` | `Msg` objects: `id, acct, folder, msgid, thread, from_name, from_addr, to, cc, subject, date, unread, flagged, body, full, atts`, `email()`, `header(name)`, `html()`, `meta()` |
| `mg.meta_get(msg)`, `mg.meta_set(msg, dict)` | per-message metadata (by Message-ID) |
| `mg.data_get/data_set/data_del/data_list(kind, key)` | small JSON records (kept by `mg export`) |
| `mg.mark(msgs, op)`, `mg.move(msgs, folder=, kind="archive"\|"trash")` | server changes (not in `on_message_synced`) |
| `mg.draft(to, subject, body, ...)` | queue a draft; **always waits for human approval** |
| `mg.notify(text)` | ntfy (if configured), UI and event stream |
| `mg.emit(type, data)` | event `plugin.<name>.<type>` in `mg events` |
| `mg.log(text)` | stderr |

Exceptions in plugin code are caught and logged; they never stop a sync, the daemon or the
approval queue.

## Without Python: run hooks and events

- Rule action `run:<command>`: the command (from your config, split like a shell would, but
  without a shell) gets the mail metadata as JSON on stdin and as `MG_*` environment
  variables (`MG_ID`, `MG_FROM`, `MG_SUBJECT`, ...). Mail content never goes into the
  command line. The environment is reduced to PATH, HOME, locale and the `MG_*` variables,
  so `password_env` values are not passed on. Timeout 60 s.
- `mg events [-f]` prints a local event stream as JSON lines: `new_mail`, `sync`, `draft`
  (queued, sent, rejected, ...), `notify` and `plugin.*`. Pipe it into a script, n8n or Home
  Assistant. `mg ui` also has Server-Sent Events at `/api/events` (needs the UI session).

## Security model

Read this before enabling a plugin you did not write.

- **Plugins are trusted code.** They run inside mailgate with your user rights. A plugin can
  read all your cached mail and, being Python, could read your files or open network
  connections. Only enable plugins you have read or trust.
- **Nothing is loaded by accident.** Only bundled plugins are on by default. A file in the
  plugins folder or an installed package is loaded only after `mg plugins enable NAME`
  (interactive terminal only), which pins its SHA-256 in `plugins.lock`. If the file changes,
  mailgate refuses to load it until you enable it again. Plugin files and the folder must be
  yours and not writable by group or others.
- **Plugins cannot send.** Every send path in mailgate (SMTP connect, sending a draft,
  approval tokens, the UI send) checks whether plugin code is running and refuses. Drafts
  created by plugins always wait for a human, even with `mode = "auto"`. This guard covers
  mailgate's own code paths; a deliberately malicious plugin could still talk SMTP itself
  with its own code, which is why plugins are trusted code.
- The API object gives no passwords, `password_cmd`, SMTP access or approval tokens.
