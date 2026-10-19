"""Browser push notifications (Web Push / VAPID) for the web chat.

Every assistant message the web platform delivers — a turn's reply, a cron
result, a `yuki-conductor send` push — fans out to each browser that
subscribed from the chat sidebar. The service worker (`web/public/sw.js`)
decides whether to actually show it: it stays quiet when a focused window is
already showing that conversation, since the in-page sound covers that case.

Push needs a secure context, so it only works when the UI is reached over
HTTPS (e.g. `tailscale serve`) or on localhost.
"""

import base64
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from yuki_conductor.config import DATA_DIR, DB_FILE

logger = logging.getLogger(__name__)

# Private key lives with the other secrets, never in workspace/ (which syncs).
VAPID_KEY_FILE = DATA_DIR / ".secrets" / "vapid-private.pem"
# Push services want a contact for the sender. Nobody is going to email it, but
# Apple rejects obviously fake domains, so keep it a well-formed address.
VAPID_SUBJECT = "mailto:yuki-conductor@users.noreply.github.com"
# How long a push service holds a message for an offline browser.
PUSH_TTL_SECONDS = 24 * 3600
# Notification bodies are a glance, not the message.
MAX_BODY_CHARS = 180

_vapid_lock = threading.Lock()
_vapid: Vapid | None = None


def _load_vapid() -> Vapid:
    """Load the VAPID key pair, generating it on first use."""
    global _vapid
    with _vapid_lock:
        if _vapid is None:
            if VAPID_KEY_FILE.is_file():
                _vapid = Vapid.from_file(str(VAPID_KEY_FILE))
            else:
                vapid = Vapid()
                vapid.generate_keys()
                VAPID_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
                VAPID_KEY_FILE.write_bytes(vapid.private_pem())
                VAPID_KEY_FILE.chmod(0o600)
                logger.info("Generated VAPID key at %s", VAPID_KEY_FILE)
                _vapid = vapid
        return _vapid


def public_key() -> str:
    """The VAPID public key as a browser's `applicationServerKey` expects it."""
    raw = _load_vapid().public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class PushSubscriptionStore:
    """Browser push subscriptions, keyed by their push-service endpoint."""

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or DB_FILE
        self._lock = threading.Lock()
        self._init_table()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(self._db_path))

    def _init_table(self) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS push_subscriptions ("
                    "endpoint TEXT PRIMARY KEY, "
                    "p256dh TEXT NOT NULL, "
                    "auth TEXT NOT NULL, "
                    "user_agent TEXT, "
                    "created_at REAL NOT NULL)"
                )
                con.commit()
            finally:
                con.close()

    def add(self, endpoint: str, p256dh: str, auth: str, user_agent: str | None = None) -> None:
        with self._lock:
            con = self._connect()
            try:
                con.execute(
                    "INSERT OR REPLACE INTO push_subscriptions "
                    "(endpoint, p256dh, auth, user_agent, created_at) VALUES (?, ?, ?, ?, ?)",
                    (endpoint, p256dh, auth, user_agent, time.time()),
                )
                con.commit()
            finally:
                con.close()

    def remove(self, endpoint: str) -> bool:
        with self._lock:
            con = self._connect()
            try:
                cur = con.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
                con.commit()
                return cur.rowcount > 0
            finally:
                con.close()

    def list_all(self) -> list[dict]:
        with self._lock:
            con = self._connect()
            try:
                rows = con.execute(
                    "SELECT endpoint, p256dh, auth FROM push_subscriptions"
                ).fetchall()
            finally:
                con.close()
        return [{"endpoint": e, "keys": {"p256dh": p, "auth": a}} for e, p, a in rows]


subscriptions = PushSubscriptionStore()


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= MAX_BODY_CHARS else flat[: MAX_BODY_CHARS - 1] + "…"


def _send_all(payload: str) -> None:
    vapid = _load_vapid()
    for sub in subscriptions.list_all():
        try:
            webpush(
                sub,
                data=payload,
                vapid_private_key=vapid,
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=PUSH_TTL_SECONDS,
                timeout=10,
            )
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (404, 410):
                # The browser unsubscribed or the subscription expired.
                subscriptions.remove(sub["endpoint"])
                logger.info("Dropped expired push subscription")
            else:
                logger.warning("Web push failed (%s): %s", status, exc)
        except Exception:
            logger.warning("Web push failed", exc_info=True)


def conversation_target(conversation_id: str) -> dict:
    return {"kind": "conversation", "id": conversation_id}


def task_target(task_id: int) -> dict:
    return {"kind": "task", "id": task_id}


def notify(target: dict, title: str, text: str) -> None:
    """Push a notification to every subscriber.

    `target` is what a click opens: `conversation_target(id)` or
    `task_target(id)`. Sends on a background thread: push services can be
    slow, and the caller is usually delivering something that shouldn't wait
    on them.
    """
    if not subscriptions.list_all():
        return
    payload = json.dumps(
        {
            "target": target,
            "title": title or "yuki-conductor",
            "body": _excerpt(text) or "New message",
        }
    )
    threading.Thread(target=_send_all, args=(payload,), daemon=True, name="web-push").start()
