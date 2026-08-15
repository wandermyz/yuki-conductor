"""Skill discovery for spawned Claude Code sessions.

yuki-conductor spawns headless ``claude -p`` runs to do work. A headless run
gets **no available-skills listing** — not for plugin skills, not for user-scope
skills, not even for project-scope skills in ``<cwd>/.claude/skills``. Skills
still *resolve* by name via the ``Skill`` tool in every one of those scopes; they
are simply never advertised. So the model can use any skill it is told about,
and will never discover one on its own.

This module closes that gap by rebuilding the listing an interactive session
would have shown, and appending it to the system prompt. Skills are grouped into
tiers, and a skill declares its tier by *where it lives*:

``YUKI``
    yuki-conductor's own capabilities — the bundled ``plugins/yuki-conductor``
    plugin plus any dirs contributed through the
    ``yuki_conductor.skill_plugins`` entry-point group. Injected with
    ``--plugin-dir`` and advertised in **every** session, whatever the cwd.

``PROJECT``
    ``<cwd>/.claude/skills``. Advertised only when the session runs in that
    project. This is where a skill belongs when it only makes sense inside one
    repo (e.g. restarting the daemon, which is meaningful only in the
    yuki-conductor checkout).

``ALWAYS``
    User-scope skills named in ``workspace/skills.yaml`` under ``always:``.
    Promoted out of the ambient tier and advertised in every session. The list
    lives in the workspace, outside the repo, so private capabilities can be
    promoted without naming them in git.

``USER``
    Everything else in ``~/.claude/skills`` — listed so the session knows the
    same skills an interactive one would, without special emphasis.

The last three tiers are only reachable because ``run_claude`` passes
``--setting-sources user,project,local``; a headless run loads no settings, and
therefore no skills, by default.

Finally, an optional personal prompt at ``workspace/system-prompt.md`` is
appended verbatim. It lives outside the repo, so it can carry usage guidance for
private capabilities (internal CLIs, MCP servers) that must not be committed
here.
"""

import importlib.metadata
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from yuki_conductor.config import (
    SKILLS_CONFIG_FILE,
    SYSTEM_PROMPT_FILE,
    USER_SKILLS_DIR,
    project_dir,
)

logger = logging.getLogger(__name__)

_ENTRY_POINT_GROUP = "yuki_conductor.skill_plugins"


class Tier(Enum):
    """Scope of a skill, which decides how prominently it is advertised."""

    YUKI = "yuki"
    PROJECT = "project"
    ALWAYS = "always"
    USER = "user"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    tier: Tier
    source: Path


# --------------------------------------------------------------------------
# Plugin directories (the YUKI tier)
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Frontmatter parsing
# --------------------------------------------------------------------------


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


def _read_skill(skill_md: Path, tier: Tier) -> Skill | None:
    """Build a Skill from a SKILL.md, or None if it can't be read."""
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Could not read skill file %s", skill_md, exc_info=True)
        return None
    return Skill(
        name=_parse_frontmatter_field(text, "name") or skill_md.parent.name,
        description=_parse_frontmatter_field(text, "description") or "",
        tier=tier,
        source=skill_md.parent,
    )


def _scan_skill_dir(root: Path, tier: Tier) -> list[Skill]:
    """Read every ``<root>/*/SKILL.md`` into a Skill of the given tier."""
    if not root.is_dir():
        return []
    found = []
    for skill_md in sorted(root.glob("*/SKILL.md")):
        skill = _read_skill(skill_md, tier)
        if skill is not None:
            found.append(skill)
    return found


# --------------------------------------------------------------------------
# Workspace config (the ALWAYS tier)
# --------------------------------------------------------------------------


def _always_skill_names() -> list[str]:
    """Names of user-scope skills to advertise in every session.

    Read from ``workspace/skills.yaml``::

        always:
          - some-internal-cli

    That file is outside the repo, so a private skill can be promoted without
    being named in git.
    """
    if not SKILLS_CONFIG_FILE.is_file():
        return []
    try:
        import yaml

        data = yaml.safe_load(SKILLS_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("Could not parse %s", SKILLS_CONFIG_FILE, exc_info=True)
        return []
    names = data.get("always") or []
    if not isinstance(names, list):
        logger.warning("%s: 'always' must be a list, got %r", SKILLS_CONFIG_FILE, type(names))
        return []
    return [str(n).strip() for n in names if str(n).strip()]


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def resolve_skills(cwd: Path | str | None = None, plugin_dirs: list[str] | None = None) -> list[Skill]:
    """Return every skill reachable in a session, tagged with its tier.

    ``cwd`` is the session's working directory; it decides which project-scope
    skills apply. Names are deduplicated across tiers in priority order
    (yuki > project > always > user), so a project skill shadowing a user skill
    of the same name is listed once, under the more specific tier.
    """
    if plugin_dirs is None:
        plugin_dirs = skill_plugin_dirs()

    collected: list[Skill] = []
    for plugin_dir in plugin_dirs:
        collected.extend(_scan_skill_dir(Path(plugin_dir) / "skills", Tier.YUKI))

    if cwd is not None:
        collected.extend(_scan_skill_dir(Path(cwd) / ".claude" / "skills", Tier.PROJECT))

    always = set(_always_skill_names())
    user_skills = _scan_skill_dir(USER_SKILLS_DIR, Tier.USER)
    for skill in user_skills:
        tier = Tier.ALWAYS if skill.name in always else Tier.USER
        collected.append(Skill(skill.name, skill.description, tier, skill.source))

    missing = always - {s.name for s in user_skills}
    for name in sorted(missing):
        logger.warning(
            "%s lists %r under 'always', but no such skill in %s",
            SKILLS_CONFIG_FILE,
            name,
            USER_SKILLS_DIR,
        )

    seen: set[str] = set()
    resolved: list[Skill] = []
    for skill in collected:
        if skill.name in seen:
            continue
        seen.add(skill.name)
        resolved.append(skill)
    return resolved


# --------------------------------------------------------------------------
# Prompt rendering
# --------------------------------------------------------------------------


_PROMPT_HEADER = (
    "You are running as an agent spawned by yuki-conductor, a personal daemon "
    "that bridges the user's chat apps to Claude Code and runs scheduled tasks. "
    "You are not being run interactively by the user.\n\n"
    "The skills below are available to you, but this session receives no "
    "automatic skill listing, so they will not appear anywhere else in your "
    "context. Invoke one by name with the `Skill` tool. When a request matches a "
    "skill's description, prefer that skill over improvising with generic tools "
    "— especially over OS-level schedulers or unrelated CLIs."
)

_TIER_HEADINGS = {
    Tier.YUKI: "## yuki-conductor capabilities (always available)",
    Tier.PROJECT: "## This project: {cwd}",
    Tier.ALWAYS: "## Always available",
    Tier.USER: "## Your other skills",
}


def _render_skills(resolved: list[Skill], cwd: Path | str | None) -> list[str]:
    """Render resolved skills as grouped markdown sections."""
    lines: list[str] = []
    for tier in Tier:
        group = [s for s in resolved if s.tier is tier]
        if not group:
            continue
        lines.append("")
        lines.append(_TIER_HEADINGS[tier].format(cwd=cwd))
        for skill in group:
            lines.append(
                f"- `{skill.name}` — {skill.description}" if skill.description else f"- `{skill.name}`"
            )
    return lines


def _workspace_prompt() -> str | None:
    """Read the optional personal prompt appended to every spawned session.

    Lives in the workspace (outside the repo), so it can carry guidance for
    private capabilities — internal CLIs, MCP servers — without any of that
    landing in git.
    """
    if not SYSTEM_PROMPT_FILE.is_file():
        return None
    text = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()
    if not text:
        return None
    logger.info("Appending workspace system prompt from %s", SYSTEM_PROMPT_FILE)
    return text


def system_prompt(
    plugin_dirs: list[str] | None = None, cwd: Path | str | None = None
) -> str:
    """Build the ``--append-system-prompt`` text for a spawned session.

    Rebuilds the available-skills listing a headless run never gets, grouped by
    tier so the model can tell yuki-conductor's own capabilities from the ones
    that merely happen to be installed. The workspace prompt is appended last.
    """
    resolved = resolve_skills(cwd=cwd, plugin_dirs=plugin_dirs)

    lines = [_PROMPT_HEADER, *_render_skills(resolved, cwd)]
    prompt = "\n".join(lines)

    workspace = _workspace_prompt()
    return f"{prompt}\n\n{workspace}" if workspace else prompt
