import os
import shutil
import sys
import time
import unittest
import unittest.mock

from mailgate import plugin, tuicore
from mailgate.clean import term_safe
from mailgate.tuicore import Controller, Frontend, KeyEngine, load_settings, split_pipeline

from .helpers import Env, make_mail

EVIL_SUBJECT = "Hi\x1b]0;pwned\x07 \x1b[2J\x1b[31mred\x1b[0m \x9b31m bidi\u202eevil\r overwrite"


def evil_mail(body: str = "x", msgid: str = "") -> bytes:
    """Escape sequences arrive RFC 2047-encoded in real attacks."""
    import base64
    enc = b"=?utf-8?b?" + base64.b64encode(EVIL_SUBJECT.encode()) + b"?="
    return make_mail("PLACEHOLDER", body, msgid=msgid).replace(b"Subject: PLACEHOLDER", b"Subject: " + enc)


class FakeUI(Frontend):
    tty = True

    def __init__(self):
        self.answers: list[str] = []
        self.asked: list[str] = []
        self.editor = None

    def ask(self, prompt, prefill="", complete=None):
        self.asked.append(prompt + prefill)
        return self.answers.pop(0) if self.answers else ""

    def edit(self, text):
        self.template = text
        return self.editor(text) if self.editor else None


class ParserTest(unittest.TestCase):
    def test_pipeline(self):
        self.assertEqual(split_pipeline(":move Archiv | mark read"), [["move", "Archiv"], ["mark", "read"]])
        self.assertEqual(split_pipeline('move "Old | Stuff"|mark flag'), [["move", "Old | Stuff"], ["mark", "flag"]])
        self.assertEqual(split_pipeline("search 'a b'"), [["search", "a b"]])
        with self.assertRaises(ValueError):
            split_pipeline("move a | | mark read")
        with self.assertRaises(ValueError):
            split_pipeline('move "unclosed')

    def test_keys(self):
        e = KeyEngine(tuicore.DEFAULT_KEYS)
        self.assertEqual(e.feed("j", "normal").command, "down")
        self.assertIsNone(e.feed("5", "normal"))
        a = e.feed("j", "normal")
        self.assertEqual((a.command, a.count), ("down", 5))
        self.assertIsNone(e.feed("g", "normal"))
        self.assertEqual(e.feed("g", "normal").command, "top")
        self.assertIsNone(e.feed("d", "normal"))
        self.assertEqual(e.feed("d", "normal").command, "trash")
        self.assertIsNone(e.feed("g", "normal"))
        self.assertIsNone(e.feed("x", "normal"))  # unknown sequence resets
        self.assertEqual(e.feed("k", "normal").command, "up")
        self.assertIsNone(e.feed("m", "normal"))
        self.assertEqual((e.feed("a", "normal").special), "mark")
        self.assertIsNone(e.feed("q", "normal"))
        a = e.feed("x", "normal")
        self.assertEqual((a.special, a.arg), ("record", "x"))
        e.recording = "x"
        self.assertEqual(e.feed("q", "normal").special, "stop")
        self.assertEqual(e.feed("q", "reader").command, "close")  # q closes the reader
        self.assertIsNone(e.feed("@", "normal"))
        self.assertEqual(e.feed("@", "normal").arg, "@")

    def test_settings(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "tui.toml"
            p.write_text('layout = "vsplit"\nthreaded = true\nhtml_viewer = "w3m -dump -T text/html"\n'
                         'on_start = ["folder Archive"]\n[keys.normal]\nx = "trash"\nj = ""\n'
                         '[colors]\nunread = "yellow,default,bold"\n')
            st = load_settings(p)
            self.assertEqual((st.layout, st.threaded, st.html_viewer), ("vsplit", True, ["w3m", "-dump", "-T", "text/html"]))
            self.assertEqual(st.keys["normal"]["x"], "trash")
            self.assertNotIn("j", st.keys["normal"])
            self.assertEqual(st.colors["unread"], "yellow,default,bold")
            p.write_text('layout = "weird"\n')
            with self.assertRaises(Exception):
                load_settings(p)
            p.write_text('nope = 1\n')
            with self.assertRaises(Exception):
                load_settings(p)

    def test_term_safe(self):
        s = term_safe(EVIL_SUBJECT, oneline=True)
        self.assertNotIn("\x1b", s)
        self.assertNotIn("\x07", s)
        self.assertNotIn("\x9b", s)
        self.assertNotIn("‮", s)
        self.assertNotIn("\r", s)
        self.assertEqual(s, "Hi red 31m bidievil overwrite")
        self.assertEqual(term_safe("a\tb\nc"), "a\tb\nc")
        self.assertEqual(term_safe("x\x1bP1;2|abc\x1b\\y"), "xy")


class ControllerTest(Env):
    def setUp(self) -> None:
        super().setUp()
        self.imap.folders.update({n: {"uv": 1, "next": 1, "msgs": []} for n in ("Archive", "Trash")})
        self.imap.add("INBOX", make_mail("Erste", "Hallo, siehe https://shop.example.com/a?utm_source=x&id=1",
                                         msgid="<1@example.org>"))
        self.imap.add("INBOX", evil_mail("body \x1b[2J\x1b]0;pwned\x07 text", msgid="<2@example.org>"))
        self.imap.add("INBOX", make_mail("Dritte", "drei", msgid="<3@example.org>"))
        self.mg("sync")
        self.ui = FakeUI()
        self.c = Controller(self.cfg, self.dir / "mail.db", tuicore.Settings(mark_read=False),
                            plugin.load(self.cfg, self.dir / "mail.db"), self.ui)
        self.c.start()

    def keys(self, *keys, source="user"):
        for k in keys:
            self.c.key(k, source=source)

    def test_list_navigation_and_escapes(self):
        lines = [self.c.list_line(it) for it in self.c.items]
        self.assertEqual(len(lines), 3)
        self.assertTrue(all("\x1b" not in ln and "\x07" not in ln for ln in lines))
        self.keys("G")
        self.assertEqual(self.c.cur, 2)
        self.keys("g", "g", "2", "j")
        self.assertEqual(self.c.cur, 2)
        evil = next(i for i, it in enumerate(self.c.items) if "red" in self.c.list_line(it))
        self.c.cur = evil
        self.keys("<Enter>")
        text = "\n".join(t for _, t in self.c.lines)
        self.assertNotIn("\x1b", text)
        self.assertIn("body  text", text)

    def test_pipeline_moves_last_and_visual(self):
        self.c.cur = 0
        self.keys("V", "j")
        self.assertEqual(len(self.c.targets()), 2)
        self.c.execute("move Archive | mark read")
        self.assertIn("2 moved to Archive", self.c.status)
        self.assertEqual(len(self.imap.folders["Archive"]["msgs"]), 2)
        self.assertTrue(all("\\Seen" in m[1] for m in self.imap.folders["Archive"]["msgs"]))  # marked before moving
        self.assertEqual(len(self.c.items), 1)
        self.c.execute("frobnicate")
        self.assertIn("unknown command", self.c.status)

    def test_marks_macros_completion(self):
        self.c.cur = 2
        self.keys("m", "a", "g", "g")
        self.assertEqual(self.c.cur, 0)
        self.keys("'", "a")
        self.assertEqual(self.c.cur, 2)
        self.keys("g", "g", "q", "z", "j", "q")
        self.assertEqual(self.c.macros["z"], ["j"])
        self.keys("@", "z")
        self.assertEqual(self.c.cur, 2)
        self.assertEqual(self.c.complete("mo"), ["move"])
        self.assertIn("Archive", self.c.complete("move Ar"))
        self.assertIn("threaded=", self.c.complete("set thr"))

    def test_settings_commands(self):
        self.c.execute("set threaded! sort=subject")
        self.assertTrue(self.c.st.threaded)
        self.assertEqual(self.c.st.sort, "subject")
        self.c.execute("map normal x trash")
        self.assertEqual(self.c.engine.keys["normal"]["x"], "trash")
        self.c.execute("set layout=bogus")
        self.assertTrue(self.c.error)

    def test_links_are_cleaned(self):
        self.c.cur = next(i for i, it in enumerate(self.c.items) if it.row["subject"] == "Erste")
        self.c.execute("open")
        self.c.execute("links")
        self.assertIn("[1] https://shop.example.com/a?id=1", [t for _, t in self.c.lines])
        opened = []
        self.c.st.opener = ["echo"]
        with unittest.mock.patch("subprocess.Popen", lambda argv, **kw: opened.append(argv)):
            self.c.execute("open-link 1")
        self.assertEqual(opened, [["echo", "https://shop.example.com/a?id=1"]])

    def test_compose_then_send_needs_typed_id(self):
        self.ui.editor = lambda t: t.replace("To: ", "To: max@example.org").replace("Subject: ", "Subject: Hallo") \
            + "Text\n"
        self.ui.answers = ["nope"]  # wrong confirmation: stays a draft
        self.c.execute("compose")
        self.assertIn("d1 queued, waiting for approval", self.c.status)
        self.assertEqual(self.smtp.sent, [])
        self.assertEqual(self.store.draft(1)["status"], "pending")
        self.assertTrue(self.ntfy.published)  # approval request only after declining
        self.ui.answers = ["d1"]
        self.c.execute("send d1")
        self.assertEqual(len(self.smtp.sent), 1)
        self.assertIn("Type d1 to send", self.ui.asked[-1])

    def test_reply_template_is_sanitized(self):
        self.c.cur = next(i for i, it in enumerate(self.c.items) if "red" in self.c.list_line(it))
        self.ui.editor = None
        self.c.execute("reply")
        self.assertNotIn("\x1b", self.ui.template)
        self.assertIn("> body  text", self.ui.template)
        self.assertIn("cancelled", self.c.status)

    def test_send_invariant(self):
        from mailgate import approve, compose
        mime, rcpts = compose.build(self.cfg.account(), ["max@example.org"], "Agent", "x")
        approve.create_draft(self.cfg, self.store, self.cfg.account(), mime, rcpts)
        self.ui.answers = ["d1"] * 10  # the "human" would say yes to anything
        self.c.execute("send d1", source="start")  # on_start
        self.c.execute("send d1", source="replay")  # macro
        self.c.macros["m"] = ["\0line\0send d1"]
        self.keys("@", "m")
        self.c.execute("map normal S send d1")
        self.keys("S", source="replay")
        self.assertEqual(self.smtp.sent, [])
        self.assertIn("needs you at the keyboard", self.c.status)
        self.ui.tty = False
        self.keys("S")  # a real key press, but no terminal
        self.assertEqual(self.smtp.sent, [])
        self.ui.tty = True
        self.ui.answers = ["D 1"]  # must be the exact id
        self.keys("S")
        self.assertEqual(self.smtp.sent, [])
        self.ui.answers = ["d1"]
        self.keys("S")  # human key press + typed id: sent
        self.assertEqual(len(self.smtp.sent), 1)

    def test_plugin_tui_hooks_cannot_send(self):
        (self.dir / "plugins").mkdir(mode=0o700)
        (self.dir / "plugins" / "t.py").write_text(
            'api_version = 1\n'
            'def setup(mg):\n'
            '    mg.tui_keymap("normal", "X", "hello world")\n'
            '    @mg.tui_command("hello", "say hello")\n'
            '    def hello(tui, args, msgs):\n'
            '        tui.run("send d1")\n'
            '        from mailgate import approve\n'
            '        approve.send_draft(mg._reg.cfg, mg._store(), 1, via="x")\n'
            '        return f"hello {args} {len(msgs)} {tui.confirm(\'really?\')}"\n'
            '    @mg.tui_statusline\n'
            '    def seg(tui):\n'
            '        return "seg:" + tui.mode\n')
        plugin.pin("t")
        from mailgate import approve, compose
        mime, rcpts = compose.build(self.cfg.account(), ["max@example.org"], "Agent", "x")
        approve.create_draft(self.cfg, self.store, self.cfg.account(), mime, rcpts)
        plugin._log = lambda m: None
        reg = plugin.load(self.cfg, self.dir / "mail.db", names=["t"])
        c = Controller(self.cfg, self.dir / "mail.db", tuicore.Settings(mark_read=False), reg, self.ui)
        c.start()
        self.ui.answers = ["d1"] * 5
        c.key("X")
        self.assertEqual(self.smtp.sent, [])
        self.assertEqual(self.store.draft(1)["status"], "pending")
        self.assertIn("seg:normal", c.statusline()[1])

    def test_html_viewer_tempfile(self):
        from email.message import EmailMessage
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = "a@example.org", "jane@example.com", "HTML"
        m.set_content("plain")
        m.add_alternative("<p>Hallo <b>HTML</b></p><script>x()</script>", subtype="html")
        self.imap.add("INBOX", m.as_bytes())
        self.mg("sync")
        self.c.load()
        self.c.cur = next(i for i, it in enumerate(self.c.items) if it.row["subject"] == "HTML")
        self.c.execute("open")
        script = self.dir / "viewer.py"
        script.write_text("import os, stat, sys\nst = os.stat(sys.argv[1])\n"
                          "print(oct(stat.S_IMODE(st.st_mode)), open(sys.argv[1]).read().count('<script'))\n")
        self.c.st.html_viewer = [sys.executable, str(script), "{file}"]
        self.c.execute("html")
        self.assertIn("0o600 0", [t for _, t in self.c.lines])


@unittest.skipUnless(sys.platform != "win32" and shutil.which("python3"), "needs a pty")
class TuiSmokeTest(Env):
    def test_runs_in_a_terminal_without_passing_escapes(self):
        import pty
        import select
        self.imap.add("INBOX", evil_mail())
        self.mg("sync")
        env = dict(os.environ, TERM="xterm", PYTHONPATH=os.getcwd())
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe(sys.executable, [sys.executable, "-m", "mailgate", "tui"], env)
        import fcntl
        import struct
        import termios
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 200, 0, 0))
        out, t0 = b"", time.time()
        try:
            while time.time() - t0 < 10 and b"LIST" not in out:
                if select.select([fd], [], [], 0.1)[0]:
                    out += os.read(fd, 65536)
            os.write(fd, b"Q")
            while time.time() - t0 < 15:
                if select.select([fd], [], [], 0.1)[0]:
                    try:
                        d = os.read(fd, 65536)
                    except OSError:
                        break
                    if not d:
                        break
                    out += d
        finally:
            _, status = os.waitpid(pid, 0)
        self.assertIn(b"LIST", out)
        self.assertIn(b"overwrite", out)
        self.assertNotIn(b"\x1b]0;pwned", out)
        self.assertNotIn(b"\x9b31m", out)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)


if __name__ == "__main__":
    unittest.main()


class DocExampleTest(Env):
    def test_tui_md_plugin_example(self):
        import re
        from pathlib import Path
        code = re.findall(r"```python\n(.*?)```", (Path(__file__).parent.parent / "TUI.md").read_text(), re.S)[0]
        (self.dir / "plugins").mkdir(mode=0o700)
        (self.dir / "plugins" / "boss.py").write_text(code)
        plugin.pin("boss")
        self.imap.add("INBOX", make_mail("Status?", "x", frm="Chef <boss@example.com>"))
        self.imap.add("INBOX", make_mail("Other", "y"))
        self.mg("sync")
        reg = plugin.load(self.cfg, self.dir / "mail.db", names=["boss"])
        c = Controller(self.cfg, self.dir / "mail.db", tuicore.Settings(mark_read=False), reg, FakeUI())
        c.start()
        self.assertEqual(c.statusline()[1], "boss: 1")
        c.key("g")
        c.key("b")
        self.assertEqual(c.view, {"kind": "search", "q": "boss@example.com"})
        self.assertEqual([it.row["subject"] for it in c.items], ["Status?"])
