"""Tiny example plugin: tags mail from your boss and adds `mg boss`.

Install: copy to ~/.config/mailgate/plugins/boss.py, chmod 600, then `mg plugins enable boss`.
Settings: [plugins.boss] address = "boss@example.com"
"""
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
