# mg tui

A terminal mail client for humans on top of the same cache, plugins and approval queue as
`mg` and `mg ui`. Modal and vim-like, configurable in `~/.config/mailgate/tui.toml`, standard
library only (curses). On Windows install `windows-curses` first.

```
mg tui
```

![mg tui with folder list, message list and reader](docs/tui.png)

## Keys

List (normal mode):

| Key | Action | Key | Action |
|---|---|---|---|
| `j` `k`, arrows | next / previous | `5j`, `3k` | with a count |
| `gg` `G` | first / last | `Ctrl-d` `Ctrl-u` | half page |
| `Enter` `l` | open | `Tab` | focus list / reader / folders |
| `h` `F` | folder list | `/` | full-text search (`n` `N` move) |
| `:` | command line (Tab completes) | `?` | all commands and keys |
| `r` `a` `f` `c` | reply, reply all, forward, write | `D` | drafts waiting for approval |
| `e` `#` `dd` | archive, Trash, Trash | `u` `s` | toggle unread, toggle flag |
| `M` | move to folder | `R` | sync |
| `V` `v` | visual selection (then `e` `#` `u` `U` `s` `M` or `:`) | `t` | threaded view on/off |
| `gl` | links of the message | `Q` `ZZ` `:q` | quit |
| `m{a-z}` `'{a-z}` | set / jump to mark | `q{a-z}` … `q`, `@{a-z}`, `@@` | record / play macro |

Reader: `j` `k` `Space` `Ctrl-d` `Ctrl-u` `gg` `G` scroll, `J` `K` next/previous message,
`/` `n` `N` find in text, `T` cleaned/full text, `H` HTML via `html_viewer`, `gl` links,
`q` `Esc` `h` back to the list.

## Commands

Everything a key does is a command, so keys, macros, `on_start` and plugins all speak the same
language. `|` chains commands that work on the same messages; moves (`move`, `archive`,
`trash`) always run last, so `:move Archiv | mark read` marks first, then moves.

`quit`, `down`, `up`, `top`, `bottom`, `open`, `close`, `next`, `prev`, `folders`,
`folder [ACCOUNT/]NAME`, `unified`, `drafts`, `search WORDS`, `find TEXT`, `visual`,
`mark read|unread|flag|unflag`, `toggle unread|flag`, `move FOLDER`, `archive`, `trash`,
`sync`, `set KEY=VALUE` / `set KEY!`, `map MODE KEY COMMAND`, `text`, `html`, `links`,
`open-link N`, `reply [all]`, `forward`, `compose`, `send dN`, `discard dN`, `help`.
Plugins add more, e.g. `remind 3d !` and `snooze tomorrow` (followup), `unsub` (unsubscribe).

## Writing and sending

`r`, `a`, `f` and `c` open `$VISUAL` / `$EDITOR` (or `editor` in tui.toml) on a template with
headers (From, To, Cc, Bcc, Subject, Attach) and the quoted message. Save and quit to create
a draft; an empty To or an unchanged file cancels. Then mg tui asks:

```
Send now? Type d7 to send, Enter keeps it as a draft for approval:
```

Only typing the draft id at the keyboard sends, the same rule as `mg approve`. Macros,
`on_start`, key mappings run from them and plugin code can never answer this prompt: the
`send` command refuses unless the key press came from you. If you press Enter, the draft
waits in the normal approval queue (ntfy, `mg ui`, `:send d7` later).

## tui.toml reference

```toml
layout = "split"          # split (list above reader) | vsplit (side by side) | full (one pane)
sidebar = true            # folder list on the left
sidebar_width = 24
split_ratio = 0.4         # share of the list pane
index_format = "{flags:3} {date:>12}  {from:22.22}  {tree}{subject}"
#   fields: flags (N new, F flagged, A answered, @ attachment), date, from, email, to, subject,
#           acct, folder, id, tree (thread lines), dup; Python format specs ({from:20.20} = cut/pad)
date_format = "%d.%m.%y"
date_today = "%H:%M"
sort = "-date"            # -date | date | from | subject | unread  (- = descending)
threaded = false
text = "clean"            # clean (quotes/signatures removed, like mg read) | full
mark_read = true          # mark mail read on the server when opened
html_viewer = ["w3m", "-dump", "-T", "text/html", "{file}"]   # argv, no shell; file is a 0600 temp file
opener = []               # how links open; default $BROWSER, else xdg-open / open
editor = []               # default $VISUAL / $EDITOR / vi
quote_intro = "On {date}, {from} wrote:"
on_start = ["set threaded!"]   # commands to run at start (cannot send)
page = 300                # messages loaded per page

[keys.normal]             # also [keys.reader], [keys.visual], [keys.folders]
x = "trash"
J = "next"
gi = "folder INBOX"
dd = ""                   # empty string removes a default binding

[colors]                  # "fg,bg[,bold|underline|dim|reverse|italic]"; default = terminal colour
unread = "white,default,bold"
selected = "black,cyan"
status = "black,white"
quote = "green,default"
#   also: normal visual statusmode header link badge_ok badge_bad badge_neutral warn folder
#         folder_current search error tree dim
```

## Three customizations

**1. mutt-like look and keys**

```toml
layout = "full"
sidebar = false
index_format = "{flags:3} {date:>8} {from:20.20} ({acct}) {subject}"
date_format = "%b %d"

[keys.normal]
d = "trash"
dd = ""
"<C-n>" = "down"
"<C-p>" = "up"
```

**2. Inbox zero: archive and mark read in one key, start in the unread view**

```toml
on_start = ["folder work/INBOX", "set sort=unread"]

[keys.normal]
E = "mark read | archive"
[keys.visual]
E = "mark read | archive"
```

**3. A small plugin: `:boss` jumps to mail from your boss, `gb` runs it, statusline shows the count**

```python
# ~/.config/mailgate/plugins/boss.py, then: mg plugins enable boss
api_version = 1

def setup(mg):
    boss = mg.settings.get("address", "boss@example.com")

    @mg.tui_command("boss", "search mail from the boss")
    def boss_cmd(tui, args, msgs):
        tui.run(f"search {boss}")

    mg.tui_keymap("normal", "gb", "boss")

    @mg.tui_statusline
    def unread(tui):
        n = len(mg.q("SELECT 1 FROM msgs WHERE from_addr=? AND unread=1", (boss,)))
        return f"boss: {n}" if n else ""
```

Plugin TUI hooks: `@mg.tui_command(name, help)` gets `(tui, args, msgs)`, where `tui` has
`status(text)`, `current()`, `selection()`, `mode`, `view`, `run(cmdline)` and
`confirm(question)` (False unless you typed yes). `mg.tui_keymap(mode, key, command)` adds a
default binding (your tui.toml wins). `@mg.tui_statusline` returns a status line segment.
Plugins cannot send: `run("send ...")` and direct send calls are refused.

## Security

- Subjects, names and bodies come from strangers. mg tui (and every `mg` command) strips
  terminal escape sequences, control characters and bidi overrides before printing, so a
  mail cannot retitle your terminal, move the cursor, clear the screen or fake output.
- HTML is shown as cleaned text. `html_viewer` gets the sanitized HTML (no scripts, no remote
  content) in a 0600 temp file, started without a shell; nothing is fetched.
- Links open only for http(s) and mailto, after tracking parameters are removed (linkclean),
  through `opener` / `$BROWSER` / xdg-open without a shell.
- Compose uses a 0600 temp file that is deleted afterwards.
