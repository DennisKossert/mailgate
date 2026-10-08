from mailgate import imapops

from .helpers import Env, make_mail


class ImapOpsTest(Env):
    def setUp(self) -> None:
        super().setUp()
        self.imap.folders.update({n: {"uv": 1, "next": 1, "msgs": []} for n in ("Trash", "Archive", "Projekte")})
        self.imap.add("INBOX", make_mail("Eins", "a", msgid="<1@example.org>"))
        self.imap.add("INBOX", make_mail("Zwei", "b", msgid="<2@example.org>"), "\\Seen")
        self.imap.add("INBOX", make_mail("Drei", "c", msgid="<3@example.org>"), "\\Deleted")  # someone else's
        self.mg("sync")
        self.accts = self.cfg.accounts

    def test_flags(self):
        imapops.set_flag(self.store, self.accts, [1, 2], "read")
        self.assertEqual(self.imap.flags("INBOX")[1], "\\Seen")
        self.assertEqual(self.store.get(1)["unread"], 0)
        imapops.set_flag(self.store, self.accts, [2], "unread")
        imapops.set_flag(self.store, self.accts, [2], "flag")
        self.assertEqual(self.imap.flags("INBOX")[2], "\\Flagged")
        self.assertEqual((self.store.get(2)["unread"], self.store.get(2)["flags"]), (1, "\\Flagged"))
        self.assertTrue(any(c.startswith("SELECT") for c in self.imap.commands))
        self.mg("sync")  # cache and server agree
        self.assertEqual(self.store.get(2)["flags"], "\\Flagged")

    def test_move_with_uid_move(self):
        n, dest = imapops.move(self.store, self.accts, [1], target="Projekte")
        self.assertEqual((n, dest), (1, "Projekte"))
        self.assertEqual([m[0] for m in self.imap.folders["INBOX"]["msgs"]], [2, 3])
        self.assertEqual(len(self.imap.folders["Projekte"]["msgs"]), 1)
        self.assertIsNone(self.store.get(1))
        self.assertIn("UID MOVE", " ".join(self.imap.commands))

    def test_trash_and_archive_special_use(self):
        n, dest = imapops.move(self.store, self.accts, [2], kind="trash")
        self.assertEqual(dest, "Trash")
        self.assertEqual(len(self.imap.folders["Trash"]["msgs"]), 1)
        n, dest = imapops.move(self.store, self.accts, [1], kind="archive")
        self.assertEqual(dest, "Archive")
        self.write_config(acct='trash_folder = "Projekte"')
        imapops.move(self.store, self.cfg.accounts, [3], kind="trash")
        self.assertEqual(len(self.imap.folders["Projekte"]["msgs"]), 1)

    def test_fallback_copy_uid_expunge_only_that_uid(self):
        self.imap.caps = ["UIDPLUS"]
        imapops.move(self.store, self.accts, [1], target="Archive")
        cmds = " ".join(self.imap.commands)
        self.assertIn("UID COPY", cmds)
        self.assertIn("UID EXPUNGE", cmds)
        self.assertEqual([m[0] for m in self.imap.folders["INBOX"]["msgs"]], [2, 3])  # 3 keeps its \Deleted

    def test_fallback_without_uidplus_never_expunges_others(self):
        self.imap.caps = []
        imapops.move(self.store, self.accts, [1], target="Archive")
        self.assertNotIn("EXPUNGE", [c.split()[0] for c in self.imap.commands])
        self.assertEqual(self.imap.flags("INBOX")[1], "\\Deleted")  # left flagged, not expunged
        self.assertEqual(len(self.imap.folders["Archive"]["msgs"]), 1)
        self.imap.folders["INBOX"]["msgs"] = [m for m in self.imap.folders["INBOX"]["msgs"] if m[0] == 2]
        self.mg("sync")
        imapops.move(self.store, self.accts, [2], target="Archive")  # now only ours is \Deleted
        self.assertNotIn(2, self.imap.flags("INBOX"))

    def test_trash_in_trash_is_noop(self):
        self.imap.add("Trash", make_mail("Alt", "x", msgid="<t@example.org>"))
        self.mg("sync", "--folders", "Trash")
        rid = self.store.one("SELECT id FROM msgs WHERE folder='Trash'")[0]
        n, _ = imapops.move(self.store, self.accts, [rid], kind="trash")
        self.assertEqual(n, 0)
        self.assertEqual(len(self.imap.folders["Trash"]["msgs"]), 1)

    def test_cli_mark_move(self):
        code, out = self.mg("mark", "1", "2", "--flag")
        self.assertEqual((code, out.strip()), (0, "2 marked flag"), self.last_err)
        code, out = self.mg("move", "1", "--archive")
        self.assertEqual(out.strip(), "1 moved to Archive")
        code, _ = self.mg("move", "2")
        self.assertEqual(code, 1)

    def test_sync_stays_read_only(self):
        self.imap.commands.clear()
        self.mg("sync")
        self.assertFalse([c for c in self.imap.commands if c.split()[0] in ("SELECT", "STORE", "COPY", "EXPUNGE")]
                         or [c for c in self.imap.commands if c.startswith("UID") and "FETCH" not in c
                             and "SEARCH" not in c])

    def test_idle_wakes(self):
        import threading
        stop, hit = threading.Event(), threading.Event()

        def changed():
            hit.set()
            stop.set()
        t = threading.Thread(target=imapops.idle, args=(self.cfg.account(), changed, stop), daemon=True)
        t.start()
        for _ in range(50):
            if self.imap.idlers:
                break
            stop.wait(0.05)
        self.imap.add("INBOX", make_mail("Neu", "n"))
        self.assertTrue(hit.wait(5))
        t.join(5)
        self.assertFalse(t.is_alive())

    def test_decode_folder_name(self):
        self.assertEqual(imapops._decode_name("Entw&APw-rfe"), "Entwürfe")
        self.assertEqual(imapops._decode_name("A&-B"), "A&B")
