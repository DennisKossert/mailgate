"""Draft queue, approval tokens, ntfy notifications and the send step."""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.request
from dataclasses import dataclass
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


@dataclass
class Queued:
    """Result of queueing a draft."""
    did: int
    state: str  # pending | scheduled | sent | failed
    note: str = ""  # fallback reason or send result
    warn: str | None = None


def check_rules(cfg: Config, acct: Account, rcpts: list[str], mime: bytes, reply_msg: int | None) -> list[str]:
    """Names of the [approval.rules] checks this draft fails (empty = all pass)."""
    r, fails = cfg.rules, []
    if r.allow_accounts is not None and acct.name not in r.allow_accounts:
        fails.append(f"allow_accounts: {acct.name} not allowed")
    bad = [a for a in rcpts if not any(fnmatch.fnmatchcase(a.lower(), p) for p in r.allow_to)]
    if bad:
        fails.append("allow_to: " + ", ".join(bad))
    if r.reply_only and reply_msg is None:
        fails.append("reply_only: not a reply")
    if r.deny_attachments and any(True for _ in BytesParser(policy=policy.default).parsebytes(mime).iter_attachments()):
        fails.append("deny_attachments: has attachments")
    return fails


def decide(cfg: Config, store: Store, acct: Account, rcpts: list[str], mime: bytes,
           reply_msg: int | None) -> tuple[str | None, str]:
    """(audit 'via' for an unattended send, '') or (None, reason it needs a human)."""
    mode = cfg.mode_for(acct)
    if mode == "manual":
        return None, ""
    if mode == "rules" and (fails := check_rules(cfg, acct, rcpts, mime, reply_msg)):
        return None, "rule failed: " + "; ".join(fails)
    if store.unattended_count(acct.name, int(time.time()) - 3600) >= cfg.max_per_hour:
        return None, f"rate limit reached ({cfg.max_per_hour}/h for {acct.name})"
    return ("auto" if mode == "auto" else "auto-rules"), ""


def _notify(cfg: Config, store: Store, did: int, mime: bytes, token: str, note: str = "",
            stop: bool = False) -> str | None:
    """Publish the approval (or Stop) notification if ntfy is configured; returns a warning on failure."""
    if not cfg.ntfy:
        return None
    try:
        ntfy_publish(cfg, ntfy_payload(cfg, did, mime, token, note, stop))
    except Exception as e:  # draft stays queued; other backends still work
        store.audit(did, "notify_failed", "ntfy", str(e)[:200])
        return f"ntfy notification failed: {e}"
    return None


def create_draft(cfg: Config, store: Store, acct: Account, mime: bytes, rcpts: list[str],
                 reply_msg: int | None = None, via: str = "cli") -> Queued:
    """Queue a rendered message, then apply the approval mode (manual, auto or rules)."""
    token = secrets.token_urlsafe(18)
    msg = BytesParser(policy=policy.default).parsebytes(mime)
    now = int(time.time())
    did = store.add_draft(acct=acct.name, created=now, expires=now + int(cfg.expiry_hours * 3600),
                          status="pending", mime=mime, sha256=sha256(mime), token_hash=sha256(token),
                          sender=acct.email, rcpts=json.dumps(rcpts), to_addr=str(msg["To"] or ""),
                          subject=str(msg["Subject"] or ""), reply_msg=reply_msg)
    store.audit(did, "queued", via, f"{len(rcpts)} rcpt, {len(mime)} bytes")
    auto, reason = decide(cfg, store, acct, rcpts, mime, reply_msg)
    if not auto:
        if reason:
            store.audit(did, "manual_fallback", "system", reason)
        return Queued(did, "pending", reason, _notify(cfg, store, did, mime, token, reason))
    if cfg.undo_seconds:
        store.db.execute("UPDATE drafts SET status='scheduled', send_at=? WHERE id=?", (now + cfg.undo_seconds, did))
        store.audit(did, "scheduled", auto, f"send in {cfg.undo_seconds}s")
        return Queued(did, "scheduled", f"auto-send in {cfg.undo_seconds}s",
                      _notify(cfg, store, did, mime, token, stop=True))
    return _send_unattended(cfg, store, did, auto)


def send_now(cfg: Config, store: Store, acct: Account, mime: bytes, rcpts: list[str],
             reply_msg: int | None = None, via: str = "ui") -> str:
    """Human send from `mg ui`: record the draft and send it right away, without the approval queue."""
    msg = BytesParser(policy=policy.default).parsebytes(mime)
    now = int(time.time())
    did = store.add_draft(acct=acct.name, created=now, expires=now + 600, status="pending", mime=mime,
                          sha256=sha256(mime), token_hash=sha256(secrets.token_urlsafe(18)), sender=acct.email,
                          rcpts=json.dumps(rcpts), to_addr=str(msg["To"] or ""), subject=str(msg["Subject"] or ""),
                          reply_msg=reply_msg)
    store.audit(did, "queued", via, f"{len(rcpts)} rcpt, {len(mime)} bytes, written by a human in the web UI")
    return send_draft(cfg, store, did, via)


def _send_unattended(cfg: Config, store: Store, did: int, via: str) -> Queued:
    d = store.draft(did)
    try:
        res = send_draft(cfg, store, did, via, note=f"mode={cfg.mode_for(cfg.account(d['acct']))}")
    except ApprovalError as e:
        ntfy_info(cfg, f"mailgate: {draft_ref(did)} automatic send failed: {e}")
        return Queued(did, "failed", str(e))
    ntfy_info(cfg, f"mailgate: {draft_ref(did)} sent automatically ({via}) to {d['to_addr']} | {d['subject']}")
    return Queued(did, "sent", res)


def process_due(cfg: Config, store: Store) -> list[str]:
    """Send scheduled drafts whose undo window is over (called by mg daemon)."""
    out = []
    for d in store.due():
        acct = cfg.account(d["acct"])
        reason = ""
        if cfg.mode_for(acct) == "manual":
            reason = "approval mode is now manual"
        elif store.unattended_count(acct.name, int(time.time()) - 3600) - _scheduled(store, acct.name) >= cfg.max_per_hour:
            reason = f"rate limit reached ({cfg.max_per_hour}/h for {acct.name})"
        if reason:  # back to manual approval with a fresh token
            token = secrets.token_urlsafe(18)
            if store.claim(d["id"], "scheduled", "pending"):
                store.db.execute("UPDATE drafts SET token_hash=? WHERE id=?", (sha256(token), d["id"]))
                store.audit(d["id"], "manual_fallback", "system", reason)
                _notify(cfg, store, d["id"], bytes(d["mime"]), token, reason)
                out.append(f"{draft_ref(d['id'])} needs approval: {reason}")
            continue
        q = _send_unattended(cfg, store, d["id"], "auto" if cfg.mode_for(acct) == "auto" else "auto-rules")
        out.append(q.note)
    return out


def _scheduled(store: Store, acct: str) -> int:
    return store.one("SELECT COUNT(*) FROM drafts WHERE acct=? AND status='scheduled'", (acct,))[0]


def ntfy_payload(cfg: Config, did: int, mime: bytes, token: str, note: str = "", stop: bool = False) -> dict:
    """ntfy message with http actions posting to the reply topic: Send/Discard, or only Stop."""
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
    if stop:
        title, actions = f"mailgate {ref}: sending automatically in {cfg.undo_seconds}s", [action(n.stop_label, "reject")]
    else:
        title = f"mailgate {ref}: approve sending?"
        actions = [action(n.approve_label, "approve"), action(n.reject_label, "reject")]
    body = (f"Not sent automatically: {note}\n\n" if note else "") + preview(mime, NTFY_MAX_BODY)
    return {"topic": n.topic, "title": title, "message": body, "priority": n.priority, "tags": ["email"],
            "actions": actions}


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


def send_draft(cfg: Config, store: Store, did: int, via: str, note: str = "") -> str:
    """Verify hash, send via SMTP, append to Sent, audit. Returns a status line."""
    store.expire()
    d = _get(store, did)
    if not (store.claim(did, "pending", "sending") or store.claim(did, "scheduled", "sending")):
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
    store.audit(did, "sent", via, "; ".join(x for x in (note, detail) if x))
    return f"{draft_ref(did)} sent to {d['to_addr']}" + (f" ({detail})" if detail else "")


def reject_draft(store: Store, did: int, via: str, action: str = "rejected") -> str:
    _get(store, did)
    if not (store.claim(did, "pending", action) or store.claim(did, "scheduled", action)):
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
