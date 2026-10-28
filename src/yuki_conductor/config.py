"""Configuration and path constants."""

import os
import re
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
PLUGINS_FILE = WORKSPACE_DIR / "plugins.yaml"
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

# Model passed to `claude --model` when a conversation pins nothing.
#
# This must be passed explicitly rather than omitted. Omitting the flag lets the
# CLI resolve its own default, which is `opus[1m]` on a fresh run — but on a
# `-r` resume it rehydrates the model from the session record instead, and that
# record stores the *canonical* id (`claude-opus-5`), which has no `[1m]`
# context-window suffix. Since almost every turn after the first is a resume,
# omitting the flag means only the first message of a conversation ever gets the
# 1M window.
CLAUDE_DEFAULT_MODEL = os.environ.get("CLAUDE_DEFAULT_MODEL", "opus[1m]")

# Agent harnesses enabled on this machine, comma-separated, in preference
# order: the first one is the default for a new conversation that names none
# (web picker, cron `harness:`). A conversation keeps the harness it started
# with. pi and Codex need their own install and config, so they are opt-in.
def parse_harnesses(raw: str | None) -> list[str]:
    return [h.strip() for h in (raw or "claude-code").split(",") if h.strip()]


ENABLED_HARNESSES = parse_harnesses(os.environ.get("HARNESSES"))

# How many taskboard tasks each harness may run at once, e.g.
# `TASKBOARD_CONCURRENCY=claude-code:10,pi:1`. Harnesses not named fall back
# to `TASKBOARD_DEFAULT_LIMITS`, then to 1. pi defaults to 1 because it
# usually runs on a single local GPU.
TASKBOARD_DEFAULT_LIMITS = {"claude-code": 10, "pi": 1, "codex": 10}


def parse_taskboard_concurrency(raw: str | None) -> dict[str, int]:
    limits: dict[str, int] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, value = entry.partition(":")
        name = name.strip()
        if not sep or not name or not value.strip().isdigit() or int(value) < 1:
            raise RuntimeError(
                f"TASKBOARD_CONCURRENCY entry {entry!r} isn't usable: use "
                "<harness>:<positive integer>, e.g. 'claude-code:10,pi:1'"
            )
        limits[name] = int(value)
    return limits


TASKBOARD_CONCURRENCY = parse_taskboard_concurrency(os.environ.get("TASKBOARD_CONCURRENCY"))


def taskboard_limit(harness: str) -> int:
    """Max concurrent taskboard tasks for `harness`."""
    return TASKBOARD_CONCURRENCY.get(harness, TASKBOARD_DEFAULT_LIMITS.get(harness, 1))

# pi (https://github.com/earendil-works/pi). Its providers live in
# ~/.pi/agent/models.json, which is personal machine config: yuki-conductor
# only names the model, as `provider/id`.
PI_BIN = os.environ.get("PI_BIN", "pi")
PI_DEFAULT_MODEL = os.environ.get("PI_DEFAULT_MODEL", "strata/qwen3.8-flash-next-iq2_xs")

# Codex (https://github.com/openai/codex). Its provider and default model live
# in ~/.codex/config.toml; CODEX_DEFAULT_MODEL overrides that model if set.
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
CODEX_DEFAULT_MODEL = os.environ.get("CODEX_DEFAULT_MODEL") or None

def channels_override() -> list[str] | None:
    """Return the `CHANNELS` env override, or None when it isn't set.

    `CHANNELS` is no longer the source of truth for which channels are on —
    ``workspace/plugins.yaml`` is (see ``plugins.discover_plugins``). It stays
    as an escape hatch for when the registry is broken or the UI unreachable,
    and it wins outright when present. "" or "none" means "no channels at all";
    order is preserved because the cron scheduler uses it as platform
    preference order.
    """
    raw_apps = os.environ.get("CHANNELS")
    if raw_apps is None:
        return None

    raw = raw_apps.strip()
    if raw == "" or raw.lower() == "none":
        return []

    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def channels() -> list[str]:
    """Return the ordered list of enabled channel names.

    The env override wins; otherwise the plugin registry decides.
    """
    override = channels_override()
    if override is not None:
        return override

    from yuki_conductor.plugins import enabled_channels

    return enabled_channels()


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


# What a frontend build reads, relative to web/. A change to any of them means
# web/dist no longer matches the checkout.
_WEB_SOURCES = ("src", "public", "index.html", "package.json", "pnpm-lock.yaml", "vite.config.ts")


def web_frontend_stale() -> bool:
    """True when web/dist is missing or older than any frontend source file.

    web/dist is never in git, so each machine builds its own; this is how the
    daemon notices a fresh clone or a pull that changed the frontend.
    """
    web_dir = project_dir() / "web"
    built = web_dir / "dist" / "index.html"
    if not built.exists():
        return True
    built_at = built.stat().st_mtime
    for name in _WEB_SOURCES:
        source = web_dir / name
        paths = source.rglob("*") if source.is_dir() else [source]
        for path in paths:
            if path.is_file() and path.stat().st_mtime > built_at:
                return True
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


def parse_slack_command_prefix(raw: str | None) -> str:
    """Validate a slash-command prefix: lowercase letters, digits and '-'."""
    prefix = (raw or "yuki").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", prefix) or prefix.endswith("-"):
        raise RuntimeError(
            f"SLACK_COMMAND_PREFIX={raw!r} isn't usable: use lowercase letters, digits "
            "and '-', e.g. 'yuki'"
        )
    return prefix


def slack_command_prefix() -> str:
    """The `/<prefix>-…` namespace for this machine's Slack commands.

    Each machine runs its own Slack app; two apps in one workspace can't both
    own `/yuki-model`, so a second machine sets e.g. SLACK_COMMAND_PREFIX=skirk
    and registers `/skirk-model` and friends instead.
    """
    return parse_slack_command_prefix(os.environ.get("SLACK_COMMAND_PREFIX"))
