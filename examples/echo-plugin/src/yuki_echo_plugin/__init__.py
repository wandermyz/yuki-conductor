"""Minimal reference channel plugin.

Implements just enough of ``MessagingPlatform`` / ``ChatAppReceiver`` to be a
working channel, without talking to any external service. Useful for verifying
the registry end to end: enable it, restart, and it shows up in /api/status.
"""

import logging

logger = logging.getLogger(__name__)


class EchoPlatform:
    name = "echo"

    def __init__(self, session_store=None, model_store=None):
        self._session_store = session_store
        self._sessions: dict[str, str] = {}

    def send(self, conversation_key, msg):
        logger.info("[echo] %s: %s", conversation_key, msg.text)

    def set_processing(self, conversation_key, message_id, on):
        pass

    def on_stream_event(self, conversation_key, event):
        pass

    def get_session_id(self, conversation_key):
        return self._sessions.get(conversation_key)

    def set_session_id(self, conversation_key, session_id, title_hint=None):
        self._sessions[conversation_key] = session_id

    def send_notification(self, text):
        logger.info("[echo] notification: %s", text)

    def start_thread(self, text, title=None):
        key = f"echo:{len(self._sessions)}"
        logger.info("[echo] new thread %s: %s", key, text)
        return key


class EchoReceiver:
    name = "echo"

    def __init__(self, session_store=None, model_store=None):
        self.platform = EchoPlatform(session_store, model_store)
        self._started = False

    def start(self):
        self._started = True
        logger.info("[echo] receiver started")

    def on_startup_complete(self):
        pass

    def stop(self):
        self._started = False

    def status(self):
        return {
            "status": "ok" if self._started else "stopped",
            "message": "Example plugin; logs instead of sending.",
        }


def create_receiver(session_store=None, model_store=None):
    return EchoReceiver(session_store, model_store)
