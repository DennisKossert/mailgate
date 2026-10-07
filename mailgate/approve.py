"""Draft queue, approval tokens, ntfy notifications and the send step."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.request
from email.parser import BytesParser
from email import policy

from . import imapsync, smtpsend
from .compose import preview
from .config import Account, Config
from .store import Store, draft_ref

NTFY_MAX_BODY = 3500
CMD_RE = re.compile(r"^\s*(approve|reject)\s+(d[0-9a-z]+)\s+(\S+)\s*$")


class ApprovalError(Exception):
    """Draft cannot be sent or rejected."""


def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def create_draft(cfg: Config, store: Store, acct: Account, mime: bytes, rcpts: list[str],
                 reply_msg: int | None = None, via: str = "cli") -> tuple[int, str | None]:
    """Queue a rendered message. Returns (draft rowid, ntfy warning or None)."""
    token = secrets.token_urlsafe(18)
    msg = BytesParser(policy=policy.default).parsebytes(mime)
    now = int(time.time())
    did = store.add_draft(acct=acct.name, created=now, expires=now + int(cfg.expiry_hours * 3600),
                          status="pending", mime=mime, sha256=sha256(mime), token_hash=sha256(token),
                          sender=acct.email, rcpts=json.dumps(rcpts), to_addr=str(msg["To"] or ""),
                          subject=str(msg["Subject"] or ""), reply_msg=reply_msg)
    store.audit(did, "queued", via, f"{len(rcpts)} rcpt, {len(mime)} bytes")
    warn = None
    if cfg.ntfy:
        try:
            ntfy_publish(cfg, ntfy_payload(cfg, did, mime, token))
        except Exception as e:  # draft stays queued; other backends still work
            warn = f"ntfy notification failed: {e}"
            store.audit(did, "notify_failed", "ntfy", str(e)[:200])
    return did, warn


def ntfy_payload(cfg: Config, did: int, mime: bytes, token: str) -> dict:
    """ntfy JSON message with Send/Discard http actions posting to the reply topic."""
    n = cfg.ntfy
    assert n
    ref = draft_ref(did)
    url = f"{n.server}/{n.reply_topic}"
    headers = n.auth_header()

    def action(label: str, verb: str) -> dict:
        a = {"action": "http", "label": label, "url": url, "method": "POST",
             "body": f"{verb} {ref} {token}", "clear": True}
        if headers:
            a["headers"] = headers
        return a
    return {"topic": n.topic, "title": f"mailgate {ref}: approve sending?",
            "message": preview(mime, NTFY_MAX_BODY), "priority": n.priority, "tags": ["email"],
            "actions": [action(n.approve_label, "approve"), action(n.reject_label, "reject")]}


def ntfy_publish(cfg: Config, payload: dict) -> None:
    n = cfg.ntfy
    assert n
    req = urllib.request.Request(n.server + "/", data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **n.auth_header()})
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def ntfy_info(cfg: Config, text: str) -> None:
    """Low-priority status message (sent/discarded); errors are ignored."""
    if cfg.ntfy:
        try:
            ntfy_publish(cfg, {"topic": cfg.ntfy.topic, "message": text, "priority": 2, "tags": ["email"]})
        except Exception:
            pass


def _get(store: Store, did: int):
    d = store.draft(did)
    if not d:
        raise ApprovalError(f"no draft {draft_ref(did)}")
    return d


def send_draft(cfg: Config, store: Store, did: int, via: str) -> str:
    """Verify hash, send via SMTP, append to Sent, audit. Returns a status line."""
    store.expire()
    d = _get(store, did)
    if not store.claim(did, "pending", "sending"):
        raise ApprovalError(f"{draft_ref(did)} is {store.draft(did)['status']}, not pending")
    mime = bytes(d["mime"])
    if not hmac.compare_digest(sha256(mime), d["sha256"]):
        store.set_status(did, "failed", "hash mismatch")
        store.audit(did, "failed", via, "hash mismatch, not sent")
        raise ApprovalError("draft content changed after queueing, not sent")
    acct = cfg.account(d["acct"])
    try:
        smtpsend.send(acct, mime, json.loads(d["rcpts"]))
    except Exception as e:
        store.set_status(did, "failed", str(e)[:500])
        store.audit(did, "failed", via, str(e)[:200])
        raise ApprovalError(f"send failed: {e}") from None
    store.set_status(did, "sent")
    detail = ""
    if acct.sent_folder:
        try:
            imapsync.append(acct, acct.sent_folder, mime)
            detail = f"appended to {acct.sent_folder}"
        except Exception as e:
            detail = f"append to {acct.sent_folder} failed: {e}"[:200]
    store.audit(did, "sent", via, detail)
    return f"{draft_ref(did)} sent to {d['to_addr']}" + (f" ({detail})" if detail else "")


def reject_draft(store: Store, did: int, via: str, action: str = "rejected") -> str:
    _get(store, did)
    if not store.claim(did, "pending", action):
        raise ApprovalError(f"{draft_ref(did)} is {store.draft(did)['status']}, not pending")
    store.audit(did, action, via)
    return f"{draft_ref(did)} {action}"


def handle_command(cfg: Config, store: Store, text: str, via: str = "ntfy") -> str:
    """Process 'approve|reject <draft> <token>' from the reply channel."""
    m = CMD_RE.match(text)
    if not m:
        return "ignored: not a command"
    verb, ref, token = m.groups()
    did = int(ref[1:], 36)
    d = store.draft(did)
    if not d or not hmac.compare_digest(sha256(token), d["token_hash"] or ""):
        store.audit(did, "bad_token", via, verb)
        return f"{ref}: invalid token, ignored"
    try:
        if verb == "approve":
            res = send_draft(cfg, store, did, via)
        else:
            res = reject_draft(store, did, via)
    except ApprovalError as e:
        res = f"{ref}: {e}"
    ntfy_info(cfg, "mailgate: " + res)
    return res
