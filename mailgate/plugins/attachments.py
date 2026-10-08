"""Rule action save_attachments:<dir>: save attachments of matching mail into a folder.

    [[rules]]
    name = "Receipts"
    subject = 'rechnung|invoice|receipt'
    action = ["save_attachments:~/Documents/Receipts", "flag"]
    save_types = ["pdf"]                         # optional, file extensions
    save_pattern = "{date}_{sender}_{name}"      # optional, also {subject} and {id}

File names are reduced to safe characters, files never leave the target folder, existing
files are never overwritten, and identical content (SHA-256) is saved only once. Nothing
is opened or executed.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

api_version = 1


def safe(s: str, n: int = 40) -> str:
    return re.sub(r"[^\w.@-]+", "_", s or "").strip("._")[:n] or "x"


def setup(mg) -> None:
    @mg.rule_action("save_attachments", options=("save_types", "save_pattern"))
    def save(msg, target, rule):
        from mailgate.clean import attachment_parts
        types = [t.lower().lstrip(".") for t in rule.extra.get("save_types", [])]
        pattern = str(rule.extra.get("save_pattern", "{date}_{sender}_{name}"))
        outdir = Path(os.path.expanduser(target)).resolve()
        outdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        n = 0
        for part in attachment_parts(msg.email()):
            name = Path((part.get_filename() or "attachment").replace("\\", "/")).name
            ext = safe(name.rsplit(".", 1)[-1].lower(), 10) if "." in name else ""
            if types and ext not in types:
                continue
            data = part.get_payload(decode=True) or b""
            sha = hashlib.sha256(data).hexdigest()
            if mg.data_get("saved", sha):
                continue
            sender = msg.from_addr.split("@")[0] if "@" in msg.from_addr else msg.from_name
            stem = safe(pattern.format_map({"date": time.strftime("%Y-%m-%d", time.localtime(msg.date or 0)),
                                            "sender": safe(sender), "name": safe(Path(name).stem, 80),
                                            "subject": safe(msg.subject), "id": msg.id}), 150)
            suffix = f".{ext}" if ext else ""
            p, i = outdir / f"{stem}{suffix}", 1
            while p.exists():
                p, i = outdir / f"{stem}-{i}{suffix}", i + 1
            if p.resolve().parent != outdir:  # belt and braces against path tricks
                raise ValueError(f"refusing to write outside {outdir}")
            with open(p, "xb") as f:  # never overwrite
                f.write(data)
            os.chmod(p, 0o600)
            mg.data_set("saved", sha, {"path": str(p), "msg": msg.id})
            n += 1
        return f"saved {n}"
