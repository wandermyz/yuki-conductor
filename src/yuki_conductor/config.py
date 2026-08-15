"""Configuration and path constants."""

import os
import shlex
import subprocess
from pathlib import Path

from dotenv import load_dotenv

DATA_DIR = Path(os.environ.get("YUKI_CONDUCTOR_DATA_DIR", Path.home() / ".yuki-conductor"))
LOG_FILE = DATA_DIR / "daemon.log"
ERR_LOG_FILE = DATA_DIR / "daemon.err.log"

# Claude Code auth/endpoint config, shared with interactive shells.
CLAUDE_ENV_FILE = Path.home() / ".zshenv.d" / "claude.zsh"

# Vars that always differ between a `zsh -f -c` child and this process; they
# say nothing about what the sourced file set.
_SHELL_NOISE_VARS = frozenset({"_", "SHLVL", "PWD", "OLDPWD"})


def _load_claude_env() -> None:
    """Import the vars exported by ``CLAUDE_ENV_FILE`` into ``os.environ``.

    The LaunchAgent starts from launchd, which never reads zsh startup files,
    so it would otherwise miss ANTHROPIC_AUTH_TOKEN and fall back to the login
    keychain — which is locked on a rebooted machine with nobody logged in at
    the console. The file is zsh rather than dotenv (it reads the token out of
    ~/.yuki-conductor/.secrets), so run it under `zsh -f` and diff the
    resulting environment instead of parsing it. Only this one file is
    sourced — not the rest of .zshenv.d, and not .zshrc.

    The endpoint is deliberately absent here: ANTHROPIC_BASE_URL lives in
    ~/.claude/settings.json under "env", so Claude Code picks it up itself no
    matter who spawned it.
    """
    if not CLAUDE_ENV_FILE.is_file():
        return
    try:
        result = subprocess.run(
            ["zsh", "-f", "-c", f"source {shlex.quote(str(CLAUDE_ENV_FILE))} && env -0"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"warning: could not load {CLAUDE_ENV_FILE}: {exc}")
        return

    for entry in result.stdout.split("\0"):
        key, sep, value = entry.partition("=")
        if sep and key not in _SHELL_NOISE_VARS and os.environ.get(key) != value:
            os.environ[key] = value


_load_claude_env()

# .env lives next to the data dir (gitignored, contains secrets).
# Loaded second so it can override the Claude env file when needed.
load_dotenv(DATA_DIR / ".env", override=True)

WORKSPACE_DIR = DATA_DIR / "workspace"
DB_FILE = WORKSPACE_DIR / "yuki-conductor.db"
CRON_FILE = WORKSPACE_DIR / "cron.yaml"
SYSTEM_PROMPT_FILE = WORKSPACE_DIR / "system-prompt.md"
SKILLS_CONFIG_FILE = WORKSPACE_DIR / "skills.yaml"
# Claude Code's user-scope skill directory, shared with interactive sessions.
USER_SKILLS_DIR = Path.home() / ".claude" / "skills"
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
