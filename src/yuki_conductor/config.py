"""Configuration and path constants."""

import os
from pathlib import Path

from dotenv import load_dotenv

DATA_DIR = Path(os.environ.get("YUKI_CONDUCTOR_DATA_DIR", Path.home() / ".yuki-conductor"))
LOG_FILE = DATA_DIR / "daemon.log"
ERR_LOG_FILE = DATA_DIR / "daemon.err.log"

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

def chat_apps() -> list[str]:
    """Return ordered list of enabled chat-app plugin names.

    Reads `CHAT_APPS` (comma-separated names; "" or "none" → empty list).
    Defaults to ``["slack_socket"]`` when the env var is unset.
    Order matters: cron scheduler uses it as platform preference order.
    """
    raw_apps = os.environ.get("CHAT_APPS")
    if raw_apps is None:
        return ["slack_socket"]

    raw = raw_apps.strip()
    if raw == "" or raw.lower() == "none":
        return []

    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def project_dir() -> Path:
    """Return the root of the yuki-conductor project."""
    return Path(__file__).resolve().parent.parent.parent


def build_web_frontend() -> bool:
    """Run `pnpm install --frozen-lockfile && pnpm build` in web/.

    Returns True on success, False on failure or skip.
    """
    import shutil
    import subprocess

    web_dir = project_dir() / "web"
    if not (web_dir / "package.json").exists():
        print("No web/package.json found; skipping web build")
        return False
    pnpm = shutil.which("pnpm")
    if not pnpm:
        print("pnpm not on PATH; skipping web build")
        return False
    try:
        print("Installing web dependencies...")
        subprocess.run([pnpm, "install", "--frozen-lockfile"], cwd=web_dir, check=True)
        print("Building web frontend...")
        subprocess.run([pnpm, "build"], cwd=web_dir, check=True)
        print("Web frontend built successfully.")
        return True
    except subprocess.CalledProcessError as exc:
        print(f"Web frontend build failed: {exc}")
        return False


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
