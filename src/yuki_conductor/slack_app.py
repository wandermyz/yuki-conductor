"""Slack Bolt event handlers + SlackSocketReceiver.

Process-level startup lives in `runtime.py`; this module only owns the
Slack-specific Bolt app and a small `ChatAppReceiver` wrapper.
"""

import logging
import os
import re
import subprocess
import threading
import time

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from yuki_conductor.config import (
    CLAUDE_BIN,
    CLAUDE_WORKING_DIR,
    slack_app_token,
    slack_bot_token,
    slack_command_prefix,
)
from yuki_conductor.formatting import markdown_to_mrkdwn
from yuki_conductor.harness import HARNESSES, default_harness, get_harness
from yuki_conductor.harness.process import run_agent
from yuki_conductor.messaging import IncomingMessage, handle_incoming_message
from yuki_conductor.messaging.slack_platform import (
    SESSION_TYPE,
    SlackPlatform,
    download_slack_files,
)
from yuki_conductor.store import (
    DEFAULT_MODEL_ARG,
    HarnessStore,
    ModelStore,
    SessionStore,
)

logger = logging.getLogger(__name__)

store = SessionStore()
model_store = ModelStore()
harness_store = HarnessStore()

# `/<prefix>-harness default` clears the channel's pin.
DEFAULT_HARNESS_ARG = "default"


def channel_harness(channel: str) -> str:
    """The harness a new thread in `channel` starts on.

    The channel's `/<prefix>-harness` pin if it's still enabled here, else the
    machine default. Existing threads ignore this; they keep their own.
    """
    pinned = harness_store.get(channel)
    return pinned if pinned in HARNESSES else default_harness()


def create_app() -> App:
    app = App(token=slack_bot_token())
    # Every slash command is `/<prefix>-…`, so two machines' Slack apps can share
    # a workspace (e.g. /yuki-model on one, /skirk-model on the other).
    prefix = slack_command_prefix()

    @app.command(f"/{prefix}-harness")
    def handle_harness_command(ack, command, respond):
        ack()
        channel = command["channel_id"]
        arg = command.get("text", "").strip().lower()
        options = ", ".join([DEFAULT_HARNESS_ARG, *HARNESSES])
        pinned = harness_store.get(channel)

        if not arg:
            current = channel_harness(channel)
            note = ""
            if pinned and pinned not in HARNESSES:
                note = f" (pinned to *{pinned}*, which isn't enabled on this machine)"
            elif not pinned:
                note = " (the default)"
            respond(
                f"New threads here use *{current}*{note}. Existing threads keep "
                f"the harness they started with.\nUsage: `/{prefix}-harness [{options}]`"
            )
            return

        if arg == DEFAULT_HARNESS_ARG:
            harness_store.clear(channel)
            respond(f"New threads here now use the default harness (*{default_harness()}*).")
            return

        if arg not in HARNESSES:
            respond(f"Harness `{arg}` isn't enabled on this machine. Options: {options}")
            return

        harness_store.set(channel, arg)
        respond(
            f"New threads here now use *{arg}*. Existing threads keep the harness "
            "they started with."
        )

    @app.command(f"/{prefix}-model")
    def handle_model_command(ack, command, respond):
        """Pin a model for the channel's current harness (see `/<prefix>-harness`)."""
        ack()
        channel = command["channel_id"]
        arg = command.get("text", "").strip().lower()
        name = channel_harness(channel)
        harness = get_harness(name)
        aliases = harness.model_aliases()
        key = model_store.key(channel, name)
        default = harness.default_label() or harness.default_model() or "its own default"

        if not aliases:
            respond(f"*{name}* has no models to pick from here; it runs {default}.")
            return

        options = ", ".join([DEFAULT_MODEL_ARG, *aliases])
        if not arg:
            pinned = model_store.get(key)
            current = next((a for a, m in aliases.items() if m == pinned), pinned)
            current = current or f"{DEFAULT_MODEL_ARG} ({default})"
            respond(
                f"Current model for *{name}*: *{current}*\n"
                f"Usage: `/{prefix}-model [{options}]`"
            )
            return

        if arg == DEFAULT_MODEL_ARG:
            model_store.clear(key)
            respond(f"*{name}* model reset to the default ({default}) for this channel.")
            return

        if arg not in aliases:
            respond(f"Unknown model `{arg}` for *{name}*. Valid options: {options}")
            return

        model_store.set(key, aliases[arg])
        respond(f"*{name}* model switched to *{arg}* (`{aliases[arg]}`) for this channel.")

    @app.command(f"/{prefix}-title")
    def handle_title_command(ack, command, respond):
        ack()
        respond("Reply with `!title <new title>` in a thread to rename it.")

    @app.command(f"/{prefix}-usage")
    def handle_usage_command(ack, command, respond, client):
        ack()

        lines = ["*Yuki Usage Stats*\n"]
        stats = store.stats()
        lines.append(
            f"*Conversations:*  {stats['total']} total  |  "
            f"{stats['last_30_days']} last 30d  |  {stats['last_7_days']} last 7d"
        )

        if stats["per_channel_30d"]:
            channel_parts = []
            for ch_id, count in stats["per_channel_30d"][:5]:
                channel_parts.append(f"<#{ch_id}>: {count}")
            lines.append(f"*By channel (30d):*  {' | '.join(channel_parts)}")

        model_rows = model_store.list_all()
        if model_rows:
            # Keys are `<channel>:<harness>`.
            model_parts = []
            for key, model in model_rows[:5]:
                ch_id, _, harness_name = key.partition(":")
                model_parts.append(f"<#{ch_id}> {harness_name}: {model}")
            lines.append(f"*Model settings:*  {' | '.join(model_parts)}")

        lines.append(
            f"*Harnesses:*  {', '.join(HARNESSES)} (default {default_harness()})  |  "
            f"this channel: {channel_harness(command['channel_id'])}"
        )

        try:
            proc = subprocess.run(
                [CLAUDE_BIN, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                cwd=CLAUDE_WORKING_DIR,
            )
            version = (proc.stdout or "").strip()
            if version:
                lines.append(f"*Claude CLI:*  {version}")
        except Exception:
            pass

        respond("\n".join(lines))

    @app.command(re.compile(rf"/{re.escape(prefix)}-.+"))
    def handle_skill_command(ack, command, respond, client):
        """Catch-all: map /<prefix>-<skill> to Claude's /<skill>."""
        ack()
        skill_name = command["command"].removeprefix(f"/{prefix}-")
        channel = command["channel_id"]
        arg = command.get("text", "").strip()
        prompt = f"/{skill_name} {arg}" if arg else f"/{skill_name}"
        model = model_store.get(model_store.key(channel, "claude-code"))

        respond(f"Running `/{skill_name}`...")
        # Forwards Claude's `/<skill>` slash syntax, so it stays on Claude Code.
        result = run_agent(get_harness("claude-code"), prompt, model=model)
        if result.text:
            respond(markdown_to_mrkdwn(result.text))

    @app.event("message")
    def handle_message(event, client):
        subtype = event.get("subtype")
        if subtype and subtype != "file_share":
            return

        text = event.get("text", "").strip()
        files = event.get("files", [])
        if not text and not files:
            return

        channel = event["channel"]
        ts = event["ts"]
        thread_ts = event.get("thread_ts")
        logger.info(
            f"Slack message channel={channel} channel_type={event.get('channel_type')} ts={ts}"
        )

        if thread_ts and text.startswith("!title "):
            new_title = text[len("!title ") :].strip()
            if new_title and store.get(thread_ts):
                store.set_title(thread_ts, new_title)
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"Title set to: *{new_title}*",
                )
            else:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text="No session found for this thread.",
                )
            return

        attachments = download_slack_files(files, slack_bot_token()) if files else []
        if not text.strip() and not attachments:
            return

        # Thread reply with no known session — ignore (matches previous behavior).
        if thread_ts and store.get(thread_ts) is None:
            return

        # Thread reply for a session owned by another platform — refuse to act.
        if thread_ts:
            existing_type = store.get_session_type(thread_ts)
            if existing_type and existing_type != SESSION_TYPE:
                logger.warning(
                    f"Ignoring Slack message for thread_ts={thread_ts}: "
                    f"session_type={existing_type!r} (not slack)"
                )
                return

        is_thread_start = thread_ts is None
        conversation_key = thread_ts or ts
        # A new thread starts on the channel's harness; a reply keeps whatever
        # its thread was created with (resolve_harness reads the row).
        harness = channel_harness(channel) if is_thread_start else None
        if is_thread_start:
            # Pre-create the row so SlackPlatform can resolve channel for reactions.
            # It records the harness now: SessionStore fixes it on the first write.
            store.set(
                conversation_key,
                value="",
                channel_id=channel,
                title=text or "(attachment)",
                session_type=SESSION_TYPE,
                harness=harness,
            )

        platform = SlackPlatform(client=client, bot_token=slack_bot_token())
        if is_thread_start:
            platform.mark_harness(conversation_key, harness)
        # The channel's pin for the harness this thread runs on.
        thread_harness = harness or store.get_harness(conversation_key) or default_harness()
        model = model_store.get(model_store.key(channel, thread_harness))
        msg = IncomingMessage(
            platform=SESSION_TYPE,
            conversation_key=conversation_key,
            message_id=ts,
            text=text,
            attachments=attachments,
            is_thread_start=is_thread_start,
            title_hint=text if is_thread_start else None,
            model=model,
            harness=harness,
        )

        threading.Thread(
            target=handle_incoming_message,
            args=(platform, msg),
            name=f"slack-msg-{ts}",
            daemon=True,
        ).start()

    return app


_MAX_CONSECUTIVE_FAILURES = 10
_FAILURE_WINDOW_SECONDS = 120
_failures: list[float] = []


def _connection_error_listener(error: Exception) -> None:
    if isinstance(error, BrokenPipeError):
        now = time.monotonic()
        _failures.append(now)
        cutoff = now - _FAILURE_WINDOW_SECONDS
        while _failures and _failures[0] < cutoff:
            _failures.pop(0)
        if len(_failures) >= _MAX_CONSECUTIVE_FAILURES:
            logger.error(
                f"Connection watchdog: {len(_failures)} BrokenPipeErrors in "
                f"{_FAILURE_WINDOW_SECONDS}s — exiting for restart"
            )
            os._exit(1)


class SlackSocketReceiver:
    """ChatAppReceiver wrapping slack-bolt Socket Mode."""

    name = "slack"

    def __init__(self) -> None:
        self._app = create_app()
        self.platform = SlackPlatform(self._app.client, slack_bot_token())
        self._handler = SocketModeHandler(self._app, slack_app_token())
        self._handler.client.on_error_listeners.append(_connection_error_listener)
        self._stopped = False
        self._identity: dict[str, str] = {}

    def start(self) -> None:
        logger.info("Connection watchdog installed")
        logger.info("Starting Slack Socket Mode receiver...")
        threading.Thread(
            target=self._handler.start, name="slack-socket", daemon=True
        ).start()

    def on_startup_complete(self) -> None:
        try:
            auth = self._app.client.auth_test()
            self._identity = {
                "team": auth.get("team", ""),
                "bot_user_id": auth.get("user_id", ""),
            }
            logger.info(
                "Slack auth ok: team=%s bot=%s",
                self._identity["team"],
                self._identity["bot_user_id"],
            )
        except Exception:
            logger.warning("Slack auth_test failed", exc_info=True)

    def stop(self) -> None:
        logger.info("Stopping Slack Socket Mode receiver...")
        self._stopped = True
        try:
            self._handler.close()
        except Exception:
            logger.warning("Failed to close Slack Socket Mode handler", exc_info=True)

    def status(self) -> dict:
        details: dict = dict(self._identity)
        details["recent_broken_pipes"] = len(_failures)

        if self._stopped:
            return {"status": "stopped", "message": "Receiver stopped", "details": details}

        try:
            connected = bool(self._handler.client.is_connected())
        except Exception as exc:
            return {
                "status": "error",
                "message": f"Could not read connection state: {exc}",
                "details": details,
            }

        details["connected"] = connected
        if connected:
            team = self._identity.get("team")
            suffix = f" to {team}" if team else ""
            return {
                "status": "ok",
                "message": f"Socket Mode connected{suffix}",
                "details": details,
            }
        return {
            "status": "error",
            "message": "Socket Mode not connected",
            "details": details,
        }


def create_receiver(session_store=None, model_store=None) -> SlackSocketReceiver:
    """Channel factory, matching the signature every plugin's factory uses.

    Slack predates the plugin registry and reads its own module-level stores,
    so the arguments are accepted and ignored rather than threaded through —
    but the signature is the plugin one, so the registry needs no special case
    for a bundled channel.
    """
    return SlackSocketReceiver()
