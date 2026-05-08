"""Configuration and path constants."""

import os
from enum import StrEnum
from pathlib import Path

from dotenv import load_dotenv

DATA_DIR = Path(os.environ.get("YUKI_CONDUCTOR_DATA_DIR", Path.home() / ".yuki-conductor"))
LOG_FILE = DATA_DIR / "daemon.log"
ERR_LOG_FILE = DATA_DIR / "daemon.err.log"
PLIST_LABEL = "com.user.yuki-conductor"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"

# .env lives next to the data dir (gitignored, contains secrets).
load_dotenv(DATA_DIR / ".env", override=True)

WORKSPACE_DIR = DATA_DIR / "workspace"
DB_FILE = WORKSPACE_DIR / "yuki-conductor.db"
CRON_FILE = WORKSPACE_DIR / "cron.yaml"
ATTACHMENTS_DIR = WORKSPACE_DIR / "attachments"
UPLOADS_DIR = WORKSPACE_DIR / "uploads"
WEB_UPLOADS_DIR = UPLOADS_DIR / "web"

CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "1800"))
CLAUDE_WORKING_DIR = os.path.expanduser(os.environ.get("CLAUDE_WORKING_DIR", "~/Projects/wandering-vibe"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

TEAMS_CLI_BIN = os.environ.get("TEAMS_CLI_BIN", "teams")
TEAMS_TEAM_ID = os.environ.get("TEAMS_TEAM_ID", "")
TEAMS_CHANNEL_ID = os.environ.get("TEAMS_CHANNEL_ID", "")
TEAMS_POLL_INTERVAL = int(os.environ.get("TEAMS_POLL_INTERVAL", "5"))


class ChatApp(StrEnum):
    SLACK_SOCKET = "slack_socket"
    TEAMS_CLI = "teams_cli"


def chat_apps() -> set[ChatApp]:
    """Return the set of enabled chat-app platforms.

    Reads `CHAT_APPS` (comma-separated app names; "" or "none" → empty set).
    Defaults to `slack_socket` when the env var is unset.
    """
    raw_apps = os.environ.get("CHAT_APPS")
    if raw_apps is None:
        return {ChatApp.SLACK_SOCKET}

    raw = raw_apps.strip()
    if raw == "" or raw.lower() == "none":
        return set()

    out: set[ChatApp] = set()
    for piece in raw.split(","):
        piece = piece.strip().lower()
        if not piece:
            continue
        try:
            out.add(ChatApp(piece))
        except ValueError as err:
            valid = ", ".join(a.value for a in ChatApp)
            raise RuntimeError(
                f"Invalid CHAT_APPS entry {piece!r}; expected one of: {valid}"
            ) from err
    return out


def slack_cron_channel() -> str:
    channel = os.environ.get("SLACK_CRON_CHANNEL", "")
    if not channel:
        raise RuntimeError("SLACK_CRON_CHANNEL not set")
    return channel


def slack_app_dm_channel() -> str:
    channel = os.environ.get("SLACK_APP_DM_CHANNEL", "")
    if not channel:
        raise RuntimeError("SLACK_APP_DM_CHANNEL not set")
    return channel


def slack_bot_token() -> str:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN not set")
    return token


def slack_app_token() -> str:
    token = os.environ.get("SLACK_APP_TOKEN", "")
    if not token:
        raise RuntimeError("SLACK_APP_TOKEN not set")
    return token
