"""`mg tui`: curses frontend. Logic lives in tuicore.py; this module only draws and reads keys."""
from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from .tuicore import Controller, Frontend, fit, load_settings, wrap

COLORS = {"default": -1, "black": 0, "red": 1, "green": 2, "yellow": 3, "blue": 4, "magenta": 5, "cyan": 6,
          "white": 7}


def keyname(k) -> str | None:
    """curses get_wch() result -> key name used in keymaps ('j', '<Enter>', '<C-d>', ...)."""
    import curses
    if isinstance(k, str):
        o = ord(k)
        if k in ("\n", "\r"):
            return "<Enter>"
        if k == "\x1b":
            return "<Esc>"
        if k == "\t":
            return "<Tab>"
        if k == " ":
            return "<Space>"
        if k in ("\x7f", "\x08"):
            return "<Backspace>"
        if 1 <= o <= 26:
            return f"<C-{chr(o + 96)}>"
        return k if k.isprintable() else None
    return {curses.KEY_DOWN: "<Down>", curses.KEY_UP: "<Up>", curses.KEY_LEFT: "<Left>",
            curses.KEY_RIGHT: "<Right>", curses.KEY_NPAGE: "<PageDown>", curses.KEY_PPAGE: "<PageUp>",
            curses.KEY_HOME: "<Home>", curses.KEY_END: "<End>", curses.KEY_ENTER: "<Enter>",
            curses.KEY_BACKSPACE: "<Backspace>", curses.KEY_BTAB: "<S-Tab>"}.get(k)


class Curses(Frontend):
    tty = True

    def __init__(self, scr, ctrl: Controller):
        import curses
        self.c, self.scr, self.ctrl = curses, scr, ctrl
        self.attr: dict[str, int] = {}
        self.setup_colors()

    # ---- colors -------------------------------------------------------------------------------
    def setup_colors(self) -> None:
        c = self.c
        has = c.has_colors()
        if has:
            c.start_color()
            try:
                c.use_default_colors()
            except c.error:
                pass
        for i, (name, spec) in enumerate(self.ctrl.st.colors.items(), 1):
            parts = [p.strip().lower() for p in spec.split(",")]
            a = 0
            for extra in parts[2:]:
                a |= {"bold": c.A_BOLD, "underline": c.A_UNDERLINE, "dim": c.A_DIM, "reverse": c.A_REVERSE,
                      "standout": c.A_STANDOUT, "italic": getattr(c, "A_ITALIC", 0)}.get(extra, 0)
            if has and i < c.COLOR_PAIRS:
                try:
                    c.init_pair(i, COLORS.get(parts[0], -1), COLORS.get(parts[1] if len(parts) > 1 else "default", -1))
                    a |= c.color_pair(i)
                except c.error:
                    pass
            elif name in ("selected", "status", "statusmode", "visual", "folder_current", "search"):
                a |= c.A_REVERSE
            self.attr[name] = a

    def put(self, y: int, x: int, text: str, w: int, style: str = "normal") -> None:
        try:
            self.scr.addstr(y, x, fit(text, w), self.attr.get(style, 0))
        except self.c.error:  # bottom-right corner
            pass

    # ---- drawing ------------------------------------------------------------------------------
    def redraw(self) -> None:
        self.draw()

    def draw(self) -> None:
        ctrl, st = self.ctrl, self.ctrl.st
        H, W = self.scr.getmaxyx()
        self.scr.erase()
        body_h = max(1, H - 2)
        x0 = 0
        show_reader = ctrl.mode == "reader" or (ctrl.msg is not None and st.layout != "full")
        if st.layout == "full":
            if ctrl.mode == "folders":
                self.draw_folders(0, 0, body_h, W)
            elif ctrl.mode == "reader":
                self.draw_reader(0, 0, body_h, W)
            else:
                self.draw_list(0, 0, body_h, W)
        else:
            if st.sidebar or ctrl.mode == "folders":
                sw = min(st.sidebar_width, W // 3)
                self.draw_folders(0, 0, body_h, sw)
                for y in range(body_h):
                    self.put(y, sw, "│", 1, "dim")
                x0 = sw + 1
            w = W - x0
            if not show_reader:
                self.draw_list(0, x0, body_h, w)
            elif st.layout == "vsplit":
                lw = max(20, int(w * st.split_ratio))
                self.draw_list(0, x0, body_h, lw)
                for y in range(body_h):
                    self.put(y, x0 + lw, "│", 1, "dim")
                self.draw_reader(0, x0 + lw + 1, body_h, w - lw - 1)
            else:
                lh = max(3, int(body_h * st.split_ratio))
                self.draw_list(0, x0, lh, w)
                self.put(lh, x0, "─" * w, w, "dim")
                self.draw_reader(lh + 1, x0, body_h - lh - 1, w)
        left, right = ctrl.statusline()
        mode, _, rest = left.strip().partition(" | ")
        self.put(H - 2, 0, " " * W, W, "status")
        self.put(H - 2, 0, f" {mode} ", len(mode) + 2, "statusmode")
        self.put(H - 2, len(mode) + 2, " " + rest, max(0, W - len(mode) - 3 - len(right)), "status")
        if right:
            self.put(H - 2, max(0, W - len(right) - 1), right, len(right) + 1, "status")
        self.put(H - 1, 0, ctrl.status, W - 1, "error" if ctrl.error else "normal")
        self.scr.refresh()

    def draw_folders(self, y0: int, x0: int, h: int, w: int) -> None:
        ctrl = self.ctrl
        for i, (label, view) in enumerate(ctrl.folders[:h]):
            if view["kind"] == "header":
                style = "header"
            elif ctrl.mode == "folders" and i == ctrl.fcur:
                style = "folder_current"
            elif view == ctrl.view:
                style = "selected"
            else:
                style = "folder"
            self.put(y0 + i, x0, " " + label, w, style)

    def draw_list(self, y0: int, x0: int, h: int, w: int) -> None:
        ctrl = self.ctrl
        if ctrl.cur < ctrl.top:
            ctrl.top = ctrl.cur
        if ctrl.cur >= ctrl.top + h:
            ctrl.top = ctrl.cur - h + 1
        sel = sorted((ctrl.anchor, ctrl.cur)) if ctrl.anchor is not None else None
        if not ctrl.items:
            self.put(y0, x0, "  (no messages)", w, "dim")
        for i in range(h):
            j = ctrl.top + i
            if j >= len(ctrl.items):
                break
            it = ctrl.items[j]
            style = "unread" if (ctrl.view["kind"] != "drafts" and it.row["unread"]) else "normal"
            if sel and sel[0] <= j <= sel[1]:
                style = "visual"
            elif j == ctrl.cur and ctrl.mode != "folders":
                style = "selected"
            self.put(y0 + i, x0, ctrl.list_line(it), w, style)

    def draw_reader(self, y0: int, x0: int, h: int, w: int) -> None:
        ctrl = self.ctrl
        lines = ctrl.reader_lines(w - 1)
        ctrl.scroll = max(0, min(ctrl.scroll, max(0, len(lines) - h)))
        for i, (style, text) in enumerate(lines[ctrl.scroll:ctrl.scroll + h]):
            y = y0 + i
            if style == "badges":
                x = x0
                for seg in text.split("  "):
                    label, _, tone = seg.rpartition("|")
                    self.put(y, x, label, min(len(label), max(0, w - (x - x0))), f"badge_{tone}")
                    x += len(label) + 2
                continue
            if ctrl.find and ctrl.find.lower() in text.lower() and style in ("normal", "quote"):
                style = "search"
            self.put(y, x0, text, w, style if style in self.attr else "normal")

    # ---- input ----------------------------------------------------------------------------------
    def getkey(self) -> str | None:
        try:
            k = self.scr.get_wch()
        except self.c.error:
            return None
        if k == self.c.KEY_RESIZE:
            return "<Resize>"
        return keyname(k)

    def ask(self, prompt: str, prefill: str = "", complete=None) -> str:
        """Line editor on the bottom row: Enter, Esc, Backspace, Ctrl-u, Tab completion."""
        H, W = self.scr.getmaxyx()
        buf = prefill
        matches: list[str] = []
        mi = -1
        self.c.curs_set(1)
        self.scr.timeout(-1)
        try:
            while True:
                shown = prompt + buf
                self.put(H - 1, 0, shown[-(W - 1):], W - 1)
                self.scr.move(H - 1, min(len(shown), W - 2))
                self.scr.refresh()
                k = self.getkey()
                if k == "<Enter>":
                    return buf
                if k in ("<Esc>", "<C-c>", "<C-g>"):
                    return ""
                if k == "<Backspace>":
                    if not buf:
                        return ""
                    buf = buf[:-1]
                elif k == "<C-u>":
                    buf = ""
                elif k == "<C-w>":
                    buf = buf.rstrip().rpartition(" ")[0] + (" " if " " in buf.rstrip() else "")
                elif k in ("<Tab>", "<S-Tab>") and complete:
                    if mi < 0:
                        matches = complete(buf)
                    if matches:
                        mi = (mi + (1 if k == "<Tab>" else -1)) % len(matches)
                        head = buf.rpartition(" ")[0] if " " in buf.split("|")[-1].strip() else \
                            buf[:len(buf) - len(buf.split("|")[-1].lstrip())]
                        sep = " " if " " in buf.split("|")[-1].strip() else ""
                        buf = (head + sep if sep else head) + matches[mi]
                        self.ctrl.say("  ".join(matches[:12]) + (" ..." if len(matches) > 12 else ""))
                        self.put(H - 2, 0, self.ctrl.status, W - 1, "dim")
                    continue
                elif k == "<Space>":
                    buf += " "
                elif k and len(k) == 1:
                    buf += k
                mi = -1
        finally:
            self.c.curs_set(0)
            self.scr.timeout(500)

    def edit(self, text: str) -> str | None:
        """Run $EDITOR (no shell) on a 0600 temp file; returns the new text or None if unchanged."""
        argv = list(self.ctrl.st.editor) or shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
        fd, path = tempfile.mkstemp(prefix="mg-compose-", suffix=".eml")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            self.c.def_prog_mode()
            self.c.endwin()
            try:
                subprocess.run(argv + [path], check=False)  # nosec B603: user's own editor, no shell
            finally:
                self.c.reset_prog_mode()
                self.scr.refresh()
            new = Path(path).read_text()
        finally:
            os.unlink(path)
        return None if new == text else new

    def loop(self) -> None:
        c = self.c
        c.curs_set(0)
        self.scr.keypad(True)
        self.scr.timeout(500)
        self.ctrl.start()
        self.draw()
        while not self.ctrl.quit:
            k = self.getkey()
            if self.ctrl.after_sync:
                self.ctrl.after_sync = False
                self.ctrl.load()
                self.ctrl.load_folders()
            if k and k != "<Resize>":
                if k == "<Esc>" and self.ctrl.engine.pending:
                    self.ctrl.engine.reset()
                else:
                    self.ctrl.key(k)
            self.draw()


def run(cfg, db) -> None:
    """Start the terminal UI (curses is imported only here)."""
    try:
        import curses
    except ImportError:
        raise SystemExit("mg tui needs curses. On Windows: pip install windows-curses") from None
    from . import plugin
    settings = load_settings()
    reg = plugin.load(cfg, db, quiet=True) if cfg.plugins else None
    os.environ.setdefault("ESCDELAY", "25")

    def main(scr):
        ui = Curses.__new__(Curses)
        ctrl = Controller(cfg, db, settings, reg, ui)
        Curses.__init__(ui, scr, ctrl)
        ui.loop()
    curses.wrapper(main)


__all__ = ["run", "keyname", "Curses", "wrap"]
