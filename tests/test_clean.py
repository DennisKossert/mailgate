import unittest

from mailgate.clean import clean, html_to_text, human_size, parse_message, short_url
from mailgate.store import b36, parse_id, parse_since, thread_key

from .helpers import make_mail

DE_REPLY = """Hallo Jane,

danke, der Termin am Donnerstag passt mir gut.

Viele Grüße
Max

--
Max Mustermann
Beispielweg 1, 12345 Musterstadt

Am 05.10.2026 um 14:02 schrieb Jane Doe <jane@example.com>:
> Hallo Max,
> passt dir Donnerstag?
"""

EN_REPLY = """Sounds good, see you then.

On Mon, Oct 5, 2026 at 2:02 PM Jane Doe <jane@example.com>
wrote:
> Does Thursday work?
>> older quote
"""

OUTLOOK = """Bitte schicken Sie mir die Unterlagen.

Mit freundlichen Grüßen
Erika Beispiel

-----Ursprüngliche Nachricht-----
Von: Jane Doe <jane@example.com>
Gesendet: Montag, 5. Oktober 2026 14:02
Betreff: Unterlagen
"""

DISCLAIMER = """The invoice is attached.

Best regards
Tom

This e-mail and any attachments are confidential and intended only for the named recipient.
If you have received this message in error, please notify the sender.

Example GmbH, Sitz der Gesellschaft: Musterstadt, Registergericht: Amtsgericht Musterstadt HRB 0000
Geschäftsführer: Erika Beispiel

Diese E-Mail enthält vertrauliche Informationen. Wenn Sie nicht der richtige Adressat sind, informieren Sie uns.
"""


class CleanTest(unittest.TestCase):
    def test_german_reply_and_signature(self):
        out = clean(DE_REPLY)
        self.assertIn("der Termin am Donnerstag passt", out)
        self.assertNotIn("schrieb", out)
        self.assertNotIn("passt dir Donnerstag", out)
        self.assertNotIn("Beispielweg", out)

    def test_english_wrapped_reply_header(self):
        out = clean(EN_REPLY)
        self.assertEqual(out, "Sounds good, see you then.")

    def test_outlook_original_message(self):
        out = clean(OUTLOOK)
        self.assertIn("Unterlagen", out)
        self.assertNotIn("Ursprüngliche", out)
        self.assertNotIn("Gesendet:", out)

    def test_disclaimers(self):
        out = clean(DISCLAIMER)
        self.assertIn("The invoice is attached.", out)
        self.assertIn("Tom", out)
        for junk in ("confidential", "Registergericht", "vertrauliche", "Geschäftsführer"):
            self.assertNotIn(junk, out)

    def test_bottom_posted_reply_keeps_answer(self):
        text = "On Mon, Oct 5, 2026 at 2:02 PM Jane wrote:\n> question?\n\nMy answer below the quote."
        self.assertEqual(clean(text), "My answer below the quote.")

    def test_mobile_tagline_and_whitespace(self):
        out = clean("Ok​   passt.\n\n\n\n\nVon meinem iPhone gesendet")
        self.assertEqual(out, "Ok passt.")

    def test_tracking_urls(self):
        out = clean("Details: https://click.example.com/track?u=abc123&id=99999999 und https://example.org/x")
        self.assertEqual(out, "Details: [link: click.example.com] und https://example.org/x")
        self.assertEqual(short_url("https://www.example.com/" + "a" * 80), "[link: example.com]")

    def test_html(self):
        html = ("<html><head><style>p{color:red}</style></head><body><p>Hallo&nbsp;Jane,</p>"
                "<p>hier der <a href='https://t.example.net/c?id=1234567890abcdef'>Link</a>.</p>"
                "<ul><li>eins</li><li>zwei</li></ul><blockquote>alter Text</blockquote>"
                "<script>alert(1)</script></body></html>")
        out = clean(html_to_text(html))
        self.assertIn("Hallo Jane,", out)
        self.assertIn("hier der Link [link: t.example.net].", out)
        self.assertIn("- eins\n- zwei", out)
        self.assertNotIn("alter Text", out)
        self.assertNotIn("color", out)
        self.assertNotIn("alert", out)
        self.assertIn("alter Text", html_to_text(html, drop_quotes=False))

    def test_parse_message(self):
        raw = make_mail("Rechnung", "Anbei die Rechnung.", msgid="<a1@example.org>", attach=b"%PDF-1.4 x" * 100)
        r = parse_message(raw)
        self.assertEqual(r["from_name"], "Max Mustermann")
        self.assertEqual(r["from_addr"], "max@example.org")
        self.assertEqual(r["subject"], "Rechnung")
        self.assertEqual(r["atts"], [("rechnung.pdf", 1000)])
        self.assertEqual(r["body"], "Anbei die Rechnung.")

    def test_html_only_message(self):
        raw = make_mail("News", "x", html="<p>Neu</p>")
        self.assertEqual(parse_message(raw)["body"], "x")  # plain part preferred
        raw = make_mail("News", "", html="<p>Neu im <b>Shop</b></p><p>Hier abbestellen</p>")
        self.assertEqual(parse_message(raw)["body"], "Neu im Shop")  # empty plain: HTML, footer dropped

    def test_human_size(self):
        self.assertEqual(human_size(500), "500B")
        self.assertEqual(human_size(2048), "2K")
        self.assertEqual(human_size(1_300_000), "1.2M")


class IdTest(unittest.TestCase):
    def test_b36_roundtrip(self):
        for n in (0, 1, 35, 36, 1295, 26_000, 10**9):
            self.assertEqual(parse_id(b36(n)), n)
        self.assertEqual(b36(26_000), "k28")
        self.assertEqual(parse_id("k28*@"), 26_000)
        self.assertEqual(parse_id("d1z", draft=True), 71)
        with self.assertRaises(ValueError):
            parse_id("x-1")

    def test_since(self):
        import time
        self.assertAlmostEqual(parse_since("7d"), time.time() - 7 * 86400, delta=5)
        self.assertGreater(parse_since("2026-10-01"), 0)
        with self.assertRaises(ValueError):
            parse_since("soon")

    def test_thread_key(self):
        self.assertEqual(thread_key("<a@x> <b@x>", "<b@x>", "<c@x>"), "<a@x>")
        self.assertEqual(thread_key("", "<b@x>", "<c@x>"), "<b@x>")
        self.assertEqual(thread_key("", "", "<c@x>"), "<c@x>")
