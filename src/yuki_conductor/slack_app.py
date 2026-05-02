"""Slack Bolt event handlers — thin adapter over messaging.SlackPlatform."""

import logging
import os
import re
import subprocess
import threading
import time

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from yuki_conductor.claude_runner import run_claude
from yuki_conductor.config import (
    CLAUDE_WORKING_DIR,
    SlackMode,
    slack_app_dm_channel,
    slack_app_token,
    slack_bot_token,
    slack_mode,
)
from yuki_conductor.cron_scheduler import start_cron_scheduler
from yuki_conductor.formatting import markdown_to_mrkdwn
from yuki_conductor.messaging import IncomingMessage, handle_incoming_message
from yuki_conductor.messaging.slack_platform import SlackPlatform, download_slack_files
from yuki_conductor.store import VALID_MODELS, ModelStore, SessionStore
from yuki_conductor.web_server import start_web_server

logger = logging.getLogger(__name__)

store = SessionStore()
model_store = ModelStore()


def create_app() -> App:
    app = App(token=slack_bot_token())

    @app.command("/yuki-model")
    def handle_model_command(ack, command, respond):
        ack()
        channel = command["channel_id"]
        arg = command.get("text", "").strip().lower()

        if not arg:
            current = model_store.get(channel) or "default (set by CLI)"
            models_list = ", ".join(sorted(VALID_MODELS))
            respond(f"Current model: *{current}*\nUsage: `/yuki-model [{models_list}]`")
            return

        if arg not in VALID_MODELS:
            models_list = ", ".join(sorted(VALID_MODELS))
            respond(f"Unknown model `{arg}`. Valid options: {models_list}")
            return

        model_store.set(channel, arg)
        respond(f"Model switched to *{arg}* for this channel.")

    @app.command("/yuki-title")
    def handle_title_command(ack, command, respond):
        ack()
        respond("Reply with `!title <new title>` in a thread to rename it.")

    @app.command("/yuki-usage")
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
            model_parts = [f"<#{k}>: {v}" for k, v in model_rows[:5]]
            lines.append(f"*Model settings:*  {' | '.join(model_parts)}")

        try:
            proc = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=CLAUDE_WORKING_DIR,
            )
            version = (proc.stdout or "").strip()
            if version:
                lines.append(f"*Claude CLI:*  {version}")
        except Exception:
            pass

        respond("\n".join(lines))

    @app.command(re.compile(r"/yuki-.+"))
    def handle_skill_command(ack, command, respond, client):
        """Catch-all: map /yuki-<skill> to Claude's /<skill>."""
        ack()
        skill_name = command["command"].removeprefix("/yuki-")
        channel = command["channel_id"]
        arg = command.get("text", "").strip()
        prompt = f"/{skill_name} {arg}" if arg else f"/{skill_name}"
        model = model_store.get(channel)

        respond(f"Running `/{skill_name}`...")
        result = run_claude(prompt, model=model)
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
        model = model_store.get(channel)
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

        is_thread_start = thread_ts is None
        conversation_key = thread_ts or ts
        if is_thread_start:
            # Pre-create the row so SlackPlatform can resolve channel for reactions.
            store.set(
                conversation_key,
                value="",
                channel_id=channel,
                title=text or "(attachment)",
                session_type="slack",
            )

        platform = SlackPlatform(client=client, bot_token=slack_bot_token())
        msg = IncomingMessage(
            platform="slack",
            conversation_key=conversation_key,
            message_id=ts,
            text=text,
            attachments=attachments,
            is_thread_start=is_thread_start,
            title_hint=text if is_thread_start else None,
            model=model,
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


def start() -> None:
    """Start yuki-conductor (blocking). Slack integration is dispatched by SLACK_MODE."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    mode = slack_mode()
    logger.info(f"Starting yuki-conductor with SLACK_MODE={mode.value}")

    if mode is SlackMode.SOCKET:
        _start_socket_mode()
    elif mode is SlackMode.TOKEN:
        _start_token_mode()
    elif mode is SlackMode.NONE:
        _start_no_slack()
    else:
        raise RuntimeError(f"Unhandled SlackMode: {mode!r}")


def _start_socket_mode() -> None:
    app = create_app()

    start_web_server()
    start_cron_scheduler(app.client)

    handler = SocketModeHandler(app, slack_app_token())
    handler.client.on_error_listeners.append(_connection_error_listener)
    logger.info("Connection watchdog installed")
    logger.info("Starting yuki-conductor in Socket Mode...")

    try:
        commit = (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=CLAUDE_WORKING_DIR,
                capture_output=True,
                text=True,
            ).stdout.strip()
            or "unknown"
        )
        app.client.chat_postMessage(
            channel=slack_app_dm_channel(),
            text=f":arrows_counterclockwise: yuki-conductor daemon restarted (commit `{commit}`).",
        )
    except Exception:
        logger.warning("Failed to send restart notification", exc_info=True)

    handler.start()


def _start_token_mode() -> None:
    raise NotImplementedError("SLACK_MODE=TOKEN is not yet implemented")


def _start_no_slack() -> None:
    start_web_server()
    start_cron_scheduler(slack_client=None)
    logger.info("Slack integration disabled (SLACK_MODE=NONE); web + cron only")
    threading.Event().wait()
