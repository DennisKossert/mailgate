"""Show a mail only once in "All inboxes" and search when the same Message-ID is in several
folders or accounts (e.g. a mail to two of your addresses, or a copy in INBOX and a label folder)."""
from __future__ import annotations

api_version = 1


def setup(mg) -> None:
    @mg.list_filter
    def dedupe(items, view):
        if view.get("kind") == "folder":
            return items
        seen: dict[str, dict] = {}
        out = []
        for it in items:
            key = it.get("mi")
            if key and key in seen:
                seen[key]["dup"] = seen[key].get("dup", 1) + 1
                continue
            if key:
                seen[key] = it
            out.append(it)
        return out
