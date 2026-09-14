"""Plugin identity, discovery, and status.

A **plugin** is a named unit that contributes capability at one or more
**injection points** — well-known extension slots the daemon owns:

``channels``
    a ``ChatAppReceiver`` factory: a chat surface the daemon listens on. The
    only injection point that is enabled/disabled in this pass.

``skills``
    a Claude Code plugin dir injected into spawned sessions via ``--plugin-dir``.

``cli``
    subcommands grafted onto the ``yuki-conductor`` CLI.

``triggers``
    planned: a source of automation events. Named here only to fix the shape.

Plugins reach the registry three ways, merged in precedence order:

1. **builtin** — shipped in-tree (Slack). Cannot be uninstalled, only disabled.
2. **path** — a directory registered in ``workspace/plugins.yaml``, declaring
   itself in a ``yuki-plugin.yaml`` manifest at its root.
3. **entry_point** — an installed package declaring
   ``yuki_conductor.chat_plugins`` / ``.skill_plugins`` / ``.cli_plugins``.
   Supported unchanged, so an installed package needs no manifest.

``discover_plugins()`` **never raises.** A plugin whose path is gone comes back
``status="missing"``; one that fails to import comes back ``status="error"``
with the message. Both still render in the UI — that is the whole point, since
the failure mode being fixed is a plugin silently vanishing.
"""

import importlib
import importlib.metadata
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from yuki_conductor.plugin_config import PluginRecord, load_records

logger = logging.getLogger(__name__)

MANIFEST_NAME = "yuki-plugin.yaml"

CHANNEL_GROUP = "yuki_conductor.chat_plugins"
SKILL_GROUP = "yuki_conductor.skill_plugins"
CLI_GROUP = "yuki_conductor.cli_plugins"

# The in-tree Slack channel, described exactly like an external plugin so
# receiver construction has one code path.
BUILTIN_SLACK = "slack"
_BUILTIN_CHANNELS = {
    BUILTIN_SLACK: ["slack_socket"],
}

Source = Literal["builtin", "path", "entry_point"]
Status = Literal["ok", "missing", "error"]


@dataclass(frozen=True)
class Channel:
    """One chat surface a plugin contributes."""

    name: str
    factory: str  # "module:attr", resolved lazily


@dataclass(frozen=True)
class PluginDescriptor:
    name: str
    description: str = ""
    version: str | None = None
    source: Source = "path"
    path: Path | None = None
    enabled: bool = False
    builtin: bool = False
    channels: list[Channel] = field(default_factory=list)
    skill_dirs: list[Path] = field(default_factory=list)
    cli_entry: str | None = None
    python_path: list[Path] = field(default_factory=list)
    status: Status = "ok"
    error: str | None = None

    @property
    def channel_names(self) -> list[str]:
        return [c.name for c in self.channels]

    @property
    def usable(self) -> bool:
        return self.enabled and self.status == "ok"


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------


def read_manifest(plugin_dir: Path) -> dict:
    """Parse ``yuki-plugin.yaml`` from a plugin directory.

    Raises ``FileNotFoundError`` if absent and ``ValueError`` if unusable, so
    the caller can turn either into a descriptor status rather than a crash.
    """
    manifest = plugin_dir / MANIFEST_NAME
    if not manifest.is_file():
        raise FileNotFoundError(f"{MANIFEST_NAME} not found in {plugin_dir}")
    with open(manifest, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or not data.get("name"):
        raise ValueError(f"{manifest} has no 'name'")
    return data


def _resolve_dirs(plugin_dir: Path, values) -> list[Path]:
    """Resolve manifest-relative paths against the plugin dir."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out = []
    for v in values:
        p = Path(v).expanduser()
        out.append(p if p.is_absolute() else (plugin_dir / p).resolve())
    return out


def _descriptor_from_manifest(record: PluginRecord, data: dict) -> PluginDescriptor:
    plugin_dir = record.path
    points = data.get("injection_points") or {}

    channels = []
    for entry in points.get("channels") or []:
        if isinstance(entry, dict) and entry.get("name") and entry.get("factory"):
            channels.append(Channel(name=entry["name"], factory=entry["factory"]))

    skills = points.get("skills") or {}
    skill_dirs = _resolve_dirs(plugin_dir, skills.get("plugin_dir")) if skills else []

    cli = points.get("cli") or {}
    cli_entry = cli.get("factory") if isinstance(cli, dict) else None

    return PluginDescriptor(
        name=data["name"],
        description=data.get("description", ""),
        version=data.get("version"),
        source="path",
        path=plugin_dir,
        enabled=record.enabled,
        channels=channels,
        skill_dirs=skill_dirs,
        cli_entry=cli_entry,
        python_path=_resolve_dirs(plugin_dir, data.get("python_path")),
    )


# --------------------------------------------------------------------------
# Import plumbing
# --------------------------------------------------------------------------


_injected_paths: set[str] = set()


def inject_python_path(desc: PluginDescriptor) -> None:
    """Make an enabled path-plugin's package importable.

    Appended rather than prepended, so a plugin can't shadow a stdlib or
    first-party module by accident. Every injection is logged.
    """
    for p in desc.python_path:
        s = str(p)
        if s in _injected_paths:
            continue
        if not p.is_dir():
            logger.warning("Plugin %r declares missing python_path %s", desc.name, s)
            continue
        sys.path.append(s)
        _injected_paths.add(s)
        logger.info("Plugin %r added %s to sys.path", desc.name, s)


def load_factory(spec: str):
    """Resolve a ``module:attr`` string to a callable."""
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise ValueError(f"Invalid factory spec {spec!r}; expected 'module:attr'")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def _check_importable(desc: PluginDescriptor) -> PluginDescriptor:
    """Verify a plugin's factories import, returning a status-stamped copy.

    Only called for plugins we intend to use. A failure is recorded, never
    raised — the UI shows the reason and startup continues without it.
    """
    inject_python_path(desc)
    for channel in desc.channels:
        try:
            load_factory(channel.factory)
        except Exception as exc:
            return _with_status(desc, "error", f"{type(exc).__name__}: {exc}")
    return desc


def _with_status(desc: PluginDescriptor, status: Status, error: str | None):
    from dataclasses import replace

    return replace(desc, status=status, error=error)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def _builtin_descriptor(record: PluginRecord) -> PluginDescriptor:
    names = _BUILTIN_CHANNELS.get(record.name, [])
    return PluginDescriptor(
        name=record.name,
        description="Slack Socket Mode — built in.",
        source="builtin",
        enabled=record.enabled,
        builtin=True,
        channels=[
            Channel(name=n, factory="yuki_conductor.slack_app:SlackSocketReceiver")
            for n in names
        ],
    )


def _path_descriptor(record: PluginRecord) -> PluginDescriptor:
    base = PluginDescriptor(
        name=record.name, source="path", path=record.path, enabled=record.enabled
    )
    if record.path is None or not record.path.is_dir():
        return _with_status(base, "missing", f"Directory not found: {record.path}")
    try:
        data = read_manifest(record.path)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        return _with_status(base, "error", str(exc))

    desc = _descriptor_from_manifest(record, data)
    # The registry's name is authoritative for lookups; flag a manifest that
    # disagrees rather than silently registering under a second identity.
    if desc.name != record.name:
        return _with_status(
            _descriptor_from_manifest(
                PluginRecord(name=record.name, enabled=record.enabled, path=record.path),
                {**data, "name": record.name},
            ),
            "error",
            f"Manifest name {desc.name!r} does not match registered name {record.name!r}",
        )
    return _check_importable(desc) if desc.enabled else desc


def _entry_point_descriptors(known: set[str]) -> list[PluginDescriptor]:
    """Describe installed packages that declare our entry-point groups.

    One package usually registers in several groups; they're merged by entry
    point name into a single plugin.
    """
    merged: dict[str, dict] = {}

    def slot(name: str) -> dict:
        return merged.setdefault(
            name, {"channels": [], "skill_dirs": [], "cli": None, "error": None}
        )

    for ep in _entry_points(CHANNEL_GROUP):
        slot(ep.name)["channels"].append(Channel(name=ep.name, factory=ep.value))
    for ep in _entry_points(SKILL_GROUP):
        entry = slot(ep.name)
        try:
            entry["skill_dirs"].append(Path(ep.load()()))
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
    for ep in _entry_points(CLI_GROUP):
        slot(ep.name)["cli"] = ep.value

    out = []
    for name, data in merged.items():
        if name in known:
            continue  # a registry record for the same name wins
        out.append(
            PluginDescriptor(
                name=name,
                description="Installed Python package.",
                source="entry_point",
                enabled=True,  # installed entry points stay on, as before
                channels=data["channels"],
                skill_dirs=data["skill_dirs"],
                cli_entry=data["cli"],
                status="error" if data["error"] else "ok",
                error=data["error"],
            )
        )
    return out


def _entry_points(group: str):
    try:
        return list(importlib.metadata.entry_points(group=group))
    except Exception:
        logger.warning("Failed to read entry points for %s", group, exc_info=True)
        return []


def discover_plugins(records: list[PluginRecord] | None = None) -> list[PluginDescriptor]:
    """Describe every known plugin. Never raises."""
    registry_error = None
    if records is None:
        try:
            records = load_records()
        except Exception as exc:
            # An unparseable registry must not read as "no plugins configured" —
            # that is the silent-disappearance failure this module exists to
            # prevent. Surface it as a pseudo-plugin the UI can show.
            logger.error("Could not read plugin registry: %s", exc)
            registry_error = f"{type(exc).__name__}: {exc}"
            records = []

    descriptors = []
    if registry_error is not None:
        descriptors.append(
            PluginDescriptor(
                name="plugins.yaml",
                description="The plugin registry file could not be parsed.",
                source="builtin",
                status="error",
                error=registry_error,
            )
        )

    for record in records:
        try:
            if record.builtin:
                descriptors.append(_builtin_descriptor(record))
            else:
                descriptors.append(_path_descriptor(record))
        except Exception as exc:
            logger.warning("Plugin %r failed to describe", record.name, exc_info=True)
            descriptors.append(
                _with_status(
                    PluginDescriptor(name=record.name, enabled=record.enabled),
                    "error",
                    f"{type(exc).__name__}: {exc}",
                )
            )

    descriptors.extend(_entry_point_descriptors({d.name for d in descriptors}))
    return descriptors


def enabled_channels(descriptors: list[PluginDescriptor] | None = None) -> list[str]:
    """Channel names contributed by enabled, healthy plugins, in registry order."""
    if descriptors is None:
        descriptors = discover_plugins()
    names: list[str] = []
    for desc in descriptors:
        if not desc.usable:
            continue
        for name in desc.channel_names:
            if name not in names:
                names.append(name)
    return names


def find_channel(name: str, descriptors=None) -> tuple[PluginDescriptor, Channel] | None:
    """Locate the plugin and channel entry providing ``name``."""
    if descriptors is None:
        descriptors = discover_plugins()
    for desc in descriptors:
        for channel in desc.channels:
            if channel.name == name:
                return desc, channel
    return None


def enabled_skill_dirs() -> list[Path]:
    """Claude Code plugin dirs contributed by enabled, healthy plugins.

    A disabled plugin contributes no skills — toggling it off also stops
    advertising its skills to spawned sessions, which is what a user expects.
    """
    dirs: list[Path] = []
    for desc in discover_plugins():
        if not desc.usable:
            continue
        inject_python_path(desc)
        for d in desc.skill_dirs:
            if d not in dirs:
                dirs.append(d)
    return dirs
