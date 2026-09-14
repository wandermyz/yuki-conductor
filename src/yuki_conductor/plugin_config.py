"""Reading and surgically editing ``workspace/plugins.yaml``.

The registry file records only *identity and enablement* — never what a plugin
contributes. Capabilities are derived at load time from each plugin's own
manifest (see ``plugins.py``); a cached copy here would be exactly the stale
state this design exists to remove.

Edits are line-based for the same reason ``cron_config.set_task_field`` is: a
PyYAML round trip reflows the document and drops comments.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from yuki_conductor.config import PLUGINS_FILE

# Fields the UI is allowed to write back into plugins.yaml.
EDITABLE_FIELDS = frozenset({"enabled"})


@dataclass
class PluginRecord:
    """One line of the registry: who it is, where it came from, is it on."""

    name: str
    enabled: bool = False
    path: Path | None = None  # None for builtins and entry-point plugins
    builtin: bool = False


def load_records(path=None) -> list[PluginRecord]:
    """Parse every valid record out of plugins.yaml. Invalid entries are skipped.

    Raises ``yaml.YAMLError`` if the *document* won't parse at all — callers
    must surface that rather than treat it as "no plugins", since an unparseable
    registry would otherwise make every plugin silently disappear.
    """
    path = path or PLUGINS_FILE
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data or "plugins" not in data:
        return []

    records = []
    for entry in data["plugins"] or []:
        try:
            raw_path = entry.get("path")
            record = PluginRecord(
                name=entry["name"],
                enabled=bool(entry.get("enabled", False)),
                path=Path(raw_path).expanduser() if raw_path else None,
                builtin=bool(entry.get("builtin", False)),
            )
        except (KeyError, AttributeError, TypeError):
            continue
        records.append(record)
    return records


def _write_default(path: Path) -> None:
    """Seed a registry containing just the built-in Slack channel, disabled."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# yuki-conductor plugin registry. See plugins.example.yaml.\n"
        "plugins:\n"
        "  - name: slack\n"
        "    builtin: true\n"
        "    enabled: false\n",
        encoding="utf-8",
    )


def ensure_file(path=None) -> Path:
    """Create plugins.yaml with the builtin seed if it doesn't exist yet."""
    path = path or PLUGINS_FILE
    if not path.exists():
        _write_default(path)
    return path


def _yaml_scalar(value: str | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_plugin_field(
    plugin_name: str, field: str, value: str | bool | None, path=None
) -> bool:
    """Set one scalar ``field`` on the record named ``plugin_name``.

    Rewrites only the affected line. ``None`` deletes the key. Returns False if
    the plugin isn't in the file.
    """
    if field not in EDITABLE_FIELDS:
        raise ValueError(f"Field {field!r} is not editable")

    path = path or PLUGINS_FILE
    if not path.exists():
        return False
    lines = open(path, encoding="utf-8").read().splitlines(keepends=True)

    start, indent = _find_block(lines, plugin_name)
    if start is None:
        return False
    end = _block_end(lines, start, indent)

    key_re = re.compile(rf"^{re.escape(indent)}{re.escape(field)}:\s")
    existing = next((i for i in range(start, end) if key_re.match(lines[i])), None)

    if value is None:
        if existing is not None:
            del lines[existing]
    else:
        new_line = f"{indent}{field}: {_yaml_scalar(value)}\n"
        if existing is not None:
            lines[existing] = new_line
        else:
            lines.insert(start + 1, new_line)

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return True


def _find_block(lines: list[str], plugin_name: str) -> tuple[int | None, str]:
    """Locate the ``- name: <plugin_name>`` line and the indent of its keys."""
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)-(\s+)name:\s*(.+?)\s*$", line)
        if m and m.group(3).strip("\"'") == plugin_name:
            return i, m.group(1) + " " * (1 + len(m.group(2)))
    return None, ""


def _block_end(lines: list[str], start: int, indent: str) -> int:
    """Index just past the record's last line."""
    dash_indent = len(indent) - 2
    for i in range(start + 1, len(lines)):
        stripped = lines[i].lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        lead = len(lines[i]) - len(stripped)
        if lead <= dash_indent and (stripped.startswith("- ") or lead < dash_indent):
            return i
    return len(lines)


def add_plugin(name: str, plugin_path: Path, path=None) -> None:
    """Append a path-registered plugin, disabled by default."""
    path = ensure_file(path)
    text = path.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    text += (
        f"  - name: {_yaml_scalar(name)}\n"
        f"    path: {_yaml_scalar(str(plugin_path))}\n"
        f"    enabled: false\n"
    )
    path.write_text(text, encoding="utf-8")


def remove_plugin(name: str, path=None) -> bool:
    """Delete a record outright. Returns False if it wasn't there."""
    path = path or PLUGINS_FILE
    if not path.exists():
        return False
    lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
    start, indent = _find_block(lines, name)
    if start is None:
        return False
    end = _block_end(lines, start, indent)
    del lines[start:end]
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return True
