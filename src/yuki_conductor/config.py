"""Configuration and path constants."""

import os
from enum import StrEnum
from pathlib import Path

from dotenv import load_dotenv

# Load .env from workspace/ (gitignored, contains secrets).
# Falls back to project root .env for backwards compatibility.
_project_root = Path(__file__).resolve().parent.parent.parent
_workspace_env = _project_root.parent / "workspace" / ".env"
_project_env = _project_root / ".env"
load_dotenv(_workspace_env if _workspace_env.exists() else _project_env, override=True)

DATA_DIR = Path(os.environ.get("YUKI_CONDUCTOR_DATA_DIR", Path.home() / ".yuki-conductor"))
LOG_FILE = DATA_DIR / "daemon.log"
ERR_LOG_FILE = DATA_DIR / "daemon.err.log"
PLIST_LABEL = "com.user.yuki-conductor"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"

WORKSPACE_DIR = _project_root.parent / "workspace"
DB_FILE = WORKSPACE_DIR / "yuki-conductor.db"
CRON_FILE = WORKSPACE_DIR / "cron.yaml"
ATTACHMENTS_DIR = WORKSPACE_DIR / "attachments"
UPLOADS_DIR = WORKSPACE_DIR / "uploads"
WEB_UPLOADS_DIR = UPLOADS_DIR / "web"

CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "1800"))
CLAUDE_WORKING_DIR = os.path.expanduser(os.environ.get("CLAUDE_WORKING_DIR", "~/Projects/wandering-vibe"))


class SlackMode(StrEnum):
    NONE = "NONE"
    SOCKET = "SOCKET"
    TOKEN = "TOKEN"


def slack_mode() -> SlackMode:
    raw = os.environ.get("SLACK_MODE", SlackMode.SOCKET).strip().upper()
    try:
        return SlackMode(raw)
    except ValueError as err:
        valid = ", ".join(m.value for m in SlackMode)
        raise RuntimeError(f"Invalid SLACK_MODE={raw!r}; expected one of: {valid}") from err


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
