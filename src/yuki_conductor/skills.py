"""Discovery of Claude Code skill plugins injected into spawned sessions.

yuki-conductor spawns headless ``claude -p`` runs to do work. Those runs need
to know they are driven by yuki-conductor — e.g. that scheduling belongs in
yuki-conductor's cron config, not the OS crontab. We teach them via Claude Code
*plugins* loaded per-session with ``--plugin-dir``.

Two sources are collected:

- the plugin bundled in this repo (``plugins/yuki-conductor``), providing the
  cron skill;
- plugins contributed by installed chat-app packages through the
  ``yuki_conductor.skill_plugins`` entry-point group. Each such entry point is a
  zero-arg callable returning the path to a plugin directory (one containing a
  ``.claude-plugin/plugin.json``). This lets an installed plugin ship its own
  skill (e.g. a messaging skill) without this public repo naming it.

Loading via ``--plugin-dir`` is session-scoped, so these skills are never
visible when the user runs Claude Code directly.
"""

import importlib.metadata
import logging
from pathlib import Path

from yuki_conductor.config import project_dir

logger = logging.getLogger(__name__)

_ENTRY_POINT_GROUP = "yuki_conductor.skill_plugins"


def _bundled_plugin_dir() -> Path:
    return project_dir() / "plugins" / "yuki-conductor"


def _is_plugin_dir(path: Path) -> bool:
    return (path / ".claude-plugin" / "plugin.json").is_file()


def skill_plugin_dirs() -> list[str]:
    """Return plugin directories to inject into spawned Claude Code sessions.

    The bundled yuki-conductor plugin comes first, followed by any directories
    contributed by installed ``yuki_conductor.skill_plugins`` entry points.
    Invalid or missing directories are skipped with a warning.
    """
    dirs: list[str] = []

    bundled = _bundled_plugin_dir()
    if _is_plugin_dir(bundled):
        dirs.append(str(bundled))
    else:
        logger.warning("Bundled skill plugin missing at %s", bundled)

    for ep in importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP):
        try:
            path = Path(ep.load()())
        except Exception:
            logger.warning("Failed to load skill plugin %r", ep.name, exc_info=True)
            continue
        if _is_plugin_dir(path):
            dirs.append(str(path))
        else:
            logger.warning(
                "Skill plugin %r returned invalid plugin dir %s", ep.name, path
            )

    return dirs


SYSTEM_PROMPT = (
    "You are running as an agent spawned by yuki-conductor, a personal daemon "
    "that bridges the user's chat apps to Claude Code and runs scheduled tasks. "
    "You are not being run interactively by the user. When a request maps to a "
    "yuki-conductor capability — scheduling recurring work, or sending messages "
    "through a connected chat app — use the injected yuki-conductor skills "
    "rather than OS-level schedulers or unrelated tools."
)
