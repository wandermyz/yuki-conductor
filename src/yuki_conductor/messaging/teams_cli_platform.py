"""Teams CLI adapter — shells out to the `teams` CLI for Microsoft Teams messaging.

Uses polling to watch a channel for new messages and thread replies.
Inbound messages are dispatched to `handle_incoming_message` just like Slack.
"""

import json
import logging
import subprocess
import threading
import time
import uuid

from yuki_conductor.config import (
    TEAMS_CHANNEL_ID,
    TEAMS_CLI_BIN,
    TEAMS_POLL_INTERVAL,
    TEAMS_TEAM_ID,
)
from yuki_conductor.messaging.platform import IncomingMessage, OutgoingMessage

logger = logging.getLogger(__name__)

SESSION_TYPE = "teams_cli"
KEY_PREFIX = "teams:"


def _run_teams(*args: str) -> dict | list | None:
    """Run the teams CLI with --output json and return parsed JSON, or None on failure."""
    cmd = ["powershell", "-NoProfile", "-Command", f"& '{TEAMS_CLI_BIN}' {' '.join(args)} --output json"]
    logger.debug(f"teams-cli: {cmd}")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if proc.returncode != 0:
            logger.error(f"teams-cli failed (rc={proc.returncode}): {proc.stderr[:500]}")
            return None
        stdout = proc.stdout.strip()
        if not stdout:
            return None
        return json.loads(stdout)
    except subprocess.TimeoutExpired:
        logger.error("teams-cli timed out")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"teams-cli returned invalid JSON: {e}")
        return None


class TeamsCliPlatform:
    """MessagingPlatform implementation backed by the teams-cli binary."""

    name = "teams_cli"

    def __init__(self) -> None:
        self._user_name: str | None = None

    def get_bot_name(self) -> str:
        """Return the display name of the authenticated user (cached)."""
        if self._user_name is None:
            result = _run_teams("status")
            if isinstance(result, list) and result:
                # status --output json returns a list of {Field, Value} rows
                for row in result:
                    if row.get("Field") == "Display Name":
                        self._user_name = row.get("Value", "")
                        break
            if isinstance(result, dict):
                self._user_name = result.get("Display Name", "")
            if self._user_name is None:
                self._user_name = ""
        return self._user_name

    def send(self, conversation_key: str, msg: OutgoingMessage) -> None:
        """Send or reply to a message in the Teams channel."""
        if not msg.text:
            return

        # conversation_key is either "teams:<msg_id>" for threads or "teams:<uuid>" for cron
        parent_msg_id = self._extract_message_id(conversation_key)

        if parent_msg_id:
            # Reply in thread
            result = _run_teams(
                "message", "reply",
                "--team", TEAMS_TEAM_ID,
                "--channel", f"'{TEAMS_CHANNEL_ID}'",
                "--message", parent_msg_id,
                "--body", f"'{_escape_ps(msg.text)}'",
            )
        else:
            # New top-level message
            result = _run_teams(
                "message", "send",
                "--team", TEAMS_TEAM_ID,
                "--channel", f"'{TEAMS_CHANNEL_ID}'",
                "--body", f"'{_escape_ps(msg.text)}'",
            )
        if result is None:
            logger.error(f"Failed to send Teams message for {conversation_key}")

    def set_processing(self, conversation_key: str, message_id: str, on: bool) -> None:
        """No-op — Teams doesn't have reactions via this CLI."""
        pass

    def get_session_id(self, conversation_key: str) -> str | None:
        from yuki_conductor.store import SessionStore

        value = SessionStore().get(conversation_key)
        return value or None

    def set_session_id(
        self, conversation_key: str, session_id: str, title_hint: str | None = None
    ) -> None:
        from yuki_conductor.store import SessionStore

        store = SessionStore()
        kwargs: dict = {"session_type": SESSION_TYPE}
        if title_hint is not None:
            kwargs["title"] = title_hint
        store.set(conversation_key, session_id, **kwargs)

    def start_thread(self, text: str, title: str | None = None) -> str:
        """Post a new top-level message and return a conversation key."""
        result = _run_teams(
            "message", "send",
            "--team", TEAMS_TEAM_ID,
            "--channel", f"'{TEAMS_CHANNEL_ID}'",
            "--body", f"'{_escape_ps(text)}'",
        )
        # Try to extract message ID from result
        msg_id = None
        if isinstance(result, dict):
            msg_id = result.get("ID") or result.get("id")

        conversation_key = f"{KEY_PREFIX}{msg_id}" if msg_id else f"{KEY_PREFIX}{uuid.uuid4().hex}"

        from yuki_conductor.store import SessionStore

        SessionStore().set(
            conversation_key,
            value="",
            title=title or text[:100],
            session_type=SESSION_TYPE,
        )
        return conversation_key

    @staticmethod
    def _extract_message_id(conversation_key: str) -> str | None:
        """Extract the Teams message ID from a conversation key like 'teams:1234567'."""
        if not conversation_key.startswith(KEY_PREFIX):
            return None
        msg_id = conversation_key[len(KEY_PREFIX):]
        # If it's a UUID (cron-generated), there's no parent message to reply to
        if len(msg_id) == 32 and msg_id.isalnum():
            return None
        return msg_id if msg_id else None


def _escape_ps(text: str) -> str:
    """Escape text for embedding in a PowerShell single-quoted string."""
    return text.replace("'", "''")


class TeamsCliReceiver:
    """Polls a Teams channel for new messages and dispatches them."""

    name = "teams_cli"

    def __init__(self) -> None:
        self.platform = TeamsCliPlatform()
        self._seen_messages: set[str] = set()
        self._seen_replies: dict[str, set[str]] = {}  # parent_msg_id -> set of reply IDs
        self._tracked_threads: set[str] = set()  # message IDs we've replied to (our threads)

    def start(self) -> None:
        if not TEAMS_TEAM_ID or not TEAMS_CHANNEL_ID:
            logger.error("TEAMS_TEAM_ID and TEAMS_CHANNEL_ID must be set for teams_cli")
            return

        logger.info(
            f"Starting Teams CLI polling receiver "
            f"(team={TEAMS_TEAM_ID}, channel={TEAMS_CHANNEL_ID}, "
            f"interval={TEAMS_POLL_INTERVAL}s)"
        )

        # Seed seen messages so we don't process history on startup
        self._seed_seen_messages()

        threading.Thread(
            target=self._poll_loop, name="teams-cli-poller", daemon=True
        ).start()

    def _seed_seen_messages(self) -> None:
        """Mark all existing messages as seen so we only react to new ones."""
        messages = _run_teams(
            "message", "list",
            "--team", TEAMS_TEAM_ID,
            "--channel", f"'{TEAMS_CHANNEL_ID}'",
            "--top", "50",
        )
        if isinstance(messages, list):
            for msg in messages:
                msg_id = msg.get("ID", "")
                if msg_id:
                    self._seen_messages.add(msg_id)
            logger.info(f"Seeded {len(self._seen_messages)} existing messages")

    def _poll_loop(self) -> None:
        bot_name = self.platform.get_bot_name()
        logger.info(f"Teams CLI bot name: {bot_name!r}")

        while True:
            try:
                self._poll_channel(bot_name)
                self._poll_threads(bot_name)
            except Exception:
                logger.error("Teams CLI poll error", exc_info=True)
            time.sleep(TEAMS_POLL_INTERVAL)

    def _poll_channel(self, bot_name: str) -> None:
        """Check for new top-level messages in the channel."""
        messages = _run_teams(
            "message", "list",
            "--team", TEAMS_TEAM_ID,
            "--channel", f"'{TEAMS_CHANNEL_ID}'",
            "--top", "10",
        )
        if not isinstance(messages, list):
            return

        for msg in messages:
            msg_id = msg.get("ID", "")
            if not msg_id or msg_id in self._seen_messages:
                continue
            self._seen_messages.add(msg_id)

            sender = msg.get("From", "")
            text = msg.get("Body", "").strip()
            if not text:
                continue

            logger.info(f"New Teams message {msg_id} from {sender}: {text[:80]}")

            conversation_key = f"{KEY_PREFIX}{msg_id}"
            self._tracked_threads.add(msg_id)
            self._seen_replies.setdefault(msg_id, set())

            # Pre-create session row
            from yuki_conductor.store import SessionStore

            SessionStore().set(
                conversation_key,
                value="",
                title=text[:100],
                session_type=SESSION_TYPE,
            )

            incoming = IncomingMessage(
                platform=SESSION_TYPE,
                conversation_key=conversation_key,
                message_id=msg_id,
                text=text,
                attachments=[],
                is_thread_start=True,
                title_hint=text[:100],
            )

            from yuki_conductor.messaging.conversation import handle_incoming_message

            threading.Thread(
                target=handle_incoming_message,
                args=(self.platform, incoming),
                name=f"teams-msg-{msg_id}",
                daemon=True,
            ).start()

    def _poll_threads(self, bot_name: str) -> None:
        """Check tracked threads for new replies from users."""
        for parent_msg_id in list(self._tracked_threads):
            replies = _run_teams(
                "message", "replies",
                "--team", TEAMS_TEAM_ID,
                "--channel", f"'{TEAMS_CHANNEL_ID}'",
                "--message", parent_msg_id,
                "--top", "10",
            )
            if not isinstance(replies, list):
                continue

            seen = self._seen_replies.setdefault(parent_msg_id, set())
            for reply in replies:
                reply_id = reply.get("ID", "")
                if not reply_id or reply_id in seen:
                    continue
                seen.add(reply_id)

                sender = reply.get("From", "")
                text = reply.get("Body", "").strip()
                if not text:
                    continue

                logger.info(
                    f"New Teams reply {reply_id} in thread {parent_msg_id} "
                    f"from {sender}: {text[:80]}"
                )

                conversation_key = f"{KEY_PREFIX}{parent_msg_id}"
                incoming = IncomingMessage(
                    platform=SESSION_TYPE,
                    conversation_key=conversation_key,
                    message_id=reply_id,
                    text=text,
                    attachments=[],
                    is_thread_start=False,
                )

                from yuki_conductor.messaging.conversation import handle_incoming_message

                threading.Thread(
                    target=handle_incoming_message,
                    args=(self.platform, incoming),
                    name=f"teams-reply-{reply_id}",
                    daemon=True,
                ).start()
