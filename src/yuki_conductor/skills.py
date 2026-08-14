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

A headless ``claude -p`` run does *not* receive the available-skills listing
that interactive sessions get, so a plugin skill is reachable by name but
undiscoverable. ``system_prompt()`` closes that gap: it reads each discovered
``SKILL.md``'s frontmatter and names the skills in the appended system prompt.

Finally, an optional personal prompt at ``workspace/system-prompt.md`` is
appended verbatim. It lives outside the repo, so it can name private
capabilities (internal CLIs, user-scope skills, MCP servers) that must not be
committed here.
"""

import importlib.metadata
import logging
from pathlib import Path

from yuki_conductor.config import SYSTEM_PROMPT_FILE, project_dir

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


_PROMPT_HEADER = (
    "You are running as an agent spawned by yuki-conductor, a personal daemon "
    "that bridges the user's chat apps to Claude Code and runs scheduled tasks. "
    "You are not being run interactively by the user. When a request maps to a "
    "yuki-conductor capability — scheduling recurring work, or sending messages "
    "through a connected chat app — use the injected skills below rather than "
    "OS-level schedulers or unrelated tools.\n\n"
    "Use `yuki-conductor-cron` to schedule any recurring or timed job — never "
    "crontab, schtasks, launchd, or a built-in scheduler.\n\n"
    "These skills are loaded for this session but are NOT listed in your "
    "available-skills context, so you must invoke them by name with the Skill "
    "tool. Available skills:"
)


def _parse_frontmatter_field(text: str, field: str) -> str | None:
    """Extract a top-level YAML scalar or folded (``>``) field from frontmatter.

    Deliberately minimal — SKILL.md frontmatter only ever uses plain scalars and
    folded blocks, so a YAML dependency isn't warranted.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            break
        if not line.startswith(f"{field}:"):
            continue
        value = line[len(field) + 1 :].strip()
        if value not in (">", "|", ">-", "|-"):
            return value.strip("'\"") or None
        folded = []
        for cont in lines[i + 1 :]:
            if cont.strip() == "---" or (cont.strip() and not cont.startswith((" ", "\t"))):
                break
            folded.append(cont.strip())
        return " ".join(p for p in folded if p) or None
    return None


def _discovered_skills(plugin_dirs: list[str]) -> list[tuple[str, str]]:
    """Return (name, description) for every SKILL.md under the given plugin dirs."""
    found: list[tuple[str, str]] = []
    for plugin_dir in plugin_dirs:
        for skill_md in sorted(Path(plugin_dir).glob("skills/*/SKILL.md")):
            text = skill_md.read_text(encoding="utf-8")
            name = _parse_frontmatter_field(text, "name") or skill_md.parent.name
            description = _parse_frontmatter_field(text, "description") or ""
            found.append((name, description))
    return found


def _workspace_prompt() -> str | None:
    """Read the optional personal prompt appended to every spawned session.

    Lives in the workspace (outside the repo), so it can name private
    capabilities — internal CLIs, user-scope skills, MCP servers — without any
    of that landing in git.
    """
    if not SYSTEM_PROMPT_FILE.is_file():
        return None
    text = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
    if not text:
        return None
    logger.info("Appending workspace system prompt from %s", SYSTEM_PROMPT_FILE)
    return text


def system_prompt(plugin_dirs: list[str] | None = None) -> str:
    """Build the ``--append-system-prompt`` text for a spawned session.

    Skills loaded via ``--plugin-dir`` resolve by name but are not advertised in
    a headless (``claude -p``) session's available-skills listing, so the model
    never discovers them on its own. Naming each one here — with its description
    — is what makes them reachable. The same applies to user-scope skills in
    ``~/.claude/skills``; those are enabled by ``--setting-sources`` and named by
    the workspace prompt, which is appended last.
    """
    if plugin_dirs is None:
        plugin_dirs = skill_plugin_dirs()

    lines = [_PROMPT_HEADER]
    for name, description in _discovered_skills(plugin_dirs):
        lines.append(f"- `{name}` — {description}" if description else f"- `{name}`")

    prompt = "\n".join(lines)
    workspace = _workspace_prompt()
    return f"{prompt}\n\n{workspace}" if workspace else prompt
