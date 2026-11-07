"""Personal model aliases and connections, scoped to each harness.

Only secret references belong in models.yaml. Credentials are read when a
turn launches, never while listing the model picker.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from yuki_conductor.config import MODELS_FILE


class ModelConfigError(ValueError):
    """An unusable selection or connection (safe to show to the user)."""


@dataclass(frozen=True)
class Connection:
    type: str
    base_url: str | None = None
    key_env: str | None = None
    key_file: Path | None = None
    oauth_token_file: Path | None = None

    def oauth_token(self) -> str | None:
        if self.oauth_token_file is None:
            return None
        try:
            value = self.oauth_token_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise ModelConfigError(
                f"Cannot read subscription OAuth token file {self.oauth_token_file}"
            ) from None
        if not value:
            raise ModelConfigError(
                f"Subscription OAuth token file {self.oauth_token_file} is empty"
            )
        if any(c in value for c in "\r\n\0"):
            raise ModelConfigError("Subscription OAuth token must be a single line")
        return value

    def api_key(self) -> str | None:
        if self.type == "subscription":
            return None
        if self.key_env:
            value = os.environ.get(self.key_env, "").strip()
            if not value:
                raise ModelConfigError(f"API key environment variable {self.key_env} is empty")
        else:
            try:
                value = self.key_file.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                raise ModelConfigError(f"Cannot read API key file {self.key_file}") from None
            if not value:
                raise ModelConfigError(f"API key file {self.key_file} is empty")
        if any(c in value for c in "\r\n\0"):
            raise ModelConfigError("API key must be a single line")
        return value


@dataclass(frozen=True)
class ModelAlias:
    id: str
    label: str
    model: str
    connection: Connection


@dataclass(frozen=True)
class HarnessModels:
    default: str
    aliases: dict[str, ModelAlias]

    def resolve(self, alias: str | None) -> ModelAlias:
        selected = alias or self.default
        if selected not in self.aliases:
            raise ModelConfigError(
                f"Unknown model alias {selected!r}; choose an alias from models.yaml"
            )
        return self.aliases[selected]


def _mapping(value, location: str, allowed: set[str] | None = None) -> dict:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ModelConfigError(f"{location} must be a mapping")
    if allowed is not None and value.keys() - allowed:
        # Don't echo unexpected fields: someone might have pasted a key there.
        raise ModelConfigError(f"{location} contains unsupported fields")
    return value


def _text(value, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelConfigError(f"{location} must be a nonempty string")
    return value.strip()


def _connection(raw, location: str, config_dir: Path, harness: str) -> Connection:
    raw = _mapping(raw, location, {"type", "base_url", "api_key", "oauth_token"})
    kind = raw.get("type")
    if kind == "subscription":
        if raw.keys() - {"type", "oauth_token"}:
            raise ModelConfigError(
                f"{location}: subscription connections cannot define endpoint settings"
            )
        if "oauth_token" not in raw:
            return Connection(type=kind)
        if harness != "claude-code":
            raise ModelConfigError(f"{location}.oauth_token is only supported for Claude Code")
        token = _mapping(raw["oauth_token"], f"{location}.oauth_token", {"file"})
        path = Path(_text(token.get("file"), f"{location}.oauth_token.file")).expanduser()
        if not path.is_absolute():
            path = config_dir / path
        return Connection(type=kind, oauth_token_file=path)
    if kind != "endpoint":
        raise ModelConfigError(f"{location}.type must be subscription or endpoint")
    if "oauth_token" in raw:
        raise ModelConfigError(f"{location}: endpoint connections cannot define subscription tokens")
    base_url = _text(raw.get("base_url"), f"{location}.base_url").rstrip("/")
    try:
        url = urlsplit(base_url)
        valid = url.scheme in ("http", "https") and url.hostname and url.port != 0
        valid = valid and not (url.username or url.password or url.query or url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ModelConfigError(f"{location}.base_url must be an HTTP(S) URL without credentials or query")
    key = _mapping(raw.get("api_key"), f"{location}.api_key", {"env", "file"})
    if len(key) != 1:
        raise ModelConfigError(f"{location}.api_key needs exactly one of env or file")
    if "env" in key:
        env = _text(key["env"], f"{location}.api_key.env")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env):
            raise ModelConfigError(f"{location}.api_key.env must name an environment variable")
        return Connection(type=kind, base_url=base_url, key_env=env)
    path = Path(_text(key["file"], f"{location}.api_key.file")).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    return Connection(type=kind, base_url=base_url, key_file=path)


def load_harness_models(harness: str, path: Path | None = None) -> HarnessModels:
    path = path or MODELS_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        # YAML errors include source lines, which could contain a pasted secret.
        raise ModelConfigError(
            f"Cannot read models configuration at {path}; see models.example.yaml"
        ) from None
    root = _mapping(data, "models.yaml", {"harness"})
    harnesses = _mapping(root.get("harness"), "harness")
    section = _mapping(harnesses.get(harness), f"harness.{harness}",
                       {"default", "connections", "aliases"})
    raw_connections = _mapping(section.get("connections"), "connections")
    connections = {
        name: _connection(raw, f"connections.{name}", path.parent, harness)
        for name, raw in raw_connections.items()
    }
    aliases = {}
    labels = set()
    for name, raw in _mapping(section.get("aliases"), "aliases").items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) or name == "default":
            raise ModelConfigError(
                "Alias IDs must use lowercase letters, digits, hyphens or underscores; "
                "default is reserved"
            )
        raw = _mapping(raw, f"aliases.{name}", {"label", "model", "connection"})
        label = _text(raw.get("label", name), f"aliases.{name}.label")
        model = _text(raw.get("model"), f"aliases.{name}.model")
        connection = _text(raw.get("connection"), f"aliases.{name}.connection")
        if connection not in connections:
            raise ModelConfigError(f"aliases.{name} names an unknown connection")
        if label.casefold() == "default" or label.casefold() in labels:
            raise ModelConfigError("Alias labels must be unique; default is reserved")
        labels.add(label.casefold())
        aliases[name] = ModelAlias(name, label, model, connections[connection])
    default = _text(section.get("default"), "default")
    result = HarnessModels(default, aliases)
    result.resolve(None)
    return result
