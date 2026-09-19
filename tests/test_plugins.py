"""Tests for plugin discovery, status reporting, and the registry file."""

import importlib.metadata
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from yuki_conductor.plugin_config import (
    PluginRecord,
    add_plugin,
    ensure_file,
    load_records,
    remove_plugin,
    set_plugin_field,
)
from yuki_conductor.plugins import (
    bundled_plugin_dir,
    discover_plugins,
    enabled_channels,
    find_channel,
)


def _write_plugin_dir(tmp_path: Path, name: str, **manifest) -> Path:
    d = tmp_path / name
    d.mkdir()
    body = {"name": name, "version": "1.0", **manifest}
    import yaml

    (d / "yuki-plugin.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    return d


def _no_entry_points():
    return patch("yuki_conductor.plugins._entry_points", return_value=[])


# ---------------------------------------------------------------- registry file


def test_ensure_file_seeds_builtin(isolated_plugin_registry):
    ensure_file()
    records = load_records()
    assert [r.name for r in records] == ["slack"]
    assert records[0].builtin is True
    assert records[0].enabled is False


def test_set_enabled_roundtrip(isolated_plugin_registry):
    ensure_file()
    assert set_plugin_field("slack", "enabled", True) is True
    assert load_records()[0].enabled is True
    assert set_plugin_field("slack", "enabled", False) is True
    assert load_records()[0].enabled is False


def test_set_field_rejects_unknown_field(isolated_plugin_registry):
    ensure_file()
    with pytest.raises(ValueError):
        set_plugin_field("slack", "path", "/tmp")


def test_set_field_unknown_plugin_returns_false(isolated_plugin_registry):
    ensure_file()
    assert set_plugin_field("nope", "enabled", True) is False


def test_edit_preserves_comments(isolated_plugin_registry):
    """Line-based editing exists precisely so surrounding bytes survive."""
    ensure_file()
    original = isolated_plugin_registry.read_text(encoding="utf-8")
    assert original.startswith("# yuki-conductor plugin registry")
    set_plugin_field("slack", "enabled", True)
    assert isolated_plugin_registry.read_text(encoding="utf-8").startswith(
        "# yuki-conductor plugin registry"
    )


def test_add_and_remove_plugin(isolated_plugin_registry, tmp_path):
    ensure_file()
    add_plugin("echo", tmp_path / "echo")
    assert [r.name for r in load_records()] == ["slack", "echo"]
    assert load_records()[1].enabled is False
    assert remove_plugin("echo") is True
    assert [r.name for r in load_records()] == ["slack"]
    assert remove_plugin("echo") is False


# ------------------------------------------------------------------- discovery


def test_unparseable_registry_is_surfaced_not_silent(isolated_plugin_registry):
    """A corrupt file must not read as "no plugins" — that hides everything."""
    isolated_plugin_registry.write_text(
        'plugins:\n  - name: "x"\n    path: "C:\\gone"\n', encoding="utf-8"
    )
    with _no_entry_points():
        descs = discover_plugins()
    assert [d.status for d in descs] == ["error"]
    assert descs[0].name == "plugins.yaml"


def test_bundled_slack_is_an_ordinary_manifest_plugin():
    """Slack has no special code path — it's plugins/slack/yuki-plugin.yaml."""
    with _no_entry_points():
        descs = discover_plugins([PluginRecord(name="slack", builtin=True, enabled=True)])
    assert len(descs) == 1
    (desc,) = descs
    assert desc.builtin is True
    assert desc.source == "builtin"
    assert desc.status == "ok", desc.error
    assert desc.channel_names == ["slack"]
    # Resolved from the repo, not from the registry record.
    assert desc.path == bundled_plugin_dir("slack")


def test_bundled_plugin_dir_has_a_manifest():
    """The bundled manifest must really exist — discovery reads it from disk."""
    assert (bundled_plugin_dir("slack") / "yuki-plugin.yaml").is_file()


def test_missing_path_is_reported_not_raised(tmp_path):
    record = PluginRecord(name="ghost", path=tmp_path / "gone", enabled=True)
    with _no_entry_points():
        (desc,) = discover_plugins([record])
    assert desc.status == "missing"
    assert "not found" in desc.error.lower()


def test_dir_without_manifest_is_an_error(tmp_path):
    d = tmp_path / "bare"
    d.mkdir()
    with _no_entry_points():
        (desc,) = discover_plugins([PluginRecord(name="bare", path=d, enabled=True)])
    assert desc.status == "error"
    assert "yuki-plugin.yaml" in desc.error


def test_manifest_name_mismatch_is_an_error(tmp_path):
    d = _write_plugin_dir(tmp_path, "real")
    with _no_entry_points():
        (desc,) = discover_plugins([PluginRecord(name="wrong", path=d, enabled=True)])
    assert desc.status == "error"
    assert "does not match" in desc.error


def test_manifest_channels_and_skills_parsed(tmp_path):
    d = _write_plugin_dir(
        tmp_path,
        "echo",
        description="Example",
        injection_points={
            "channels": [{"name": "echo", "factory": "yuki_echo_plugin:create_receiver"}],
            "skills": {"plugin_dir": "./claude-plugin"},
        },
    )
    record = PluginRecord(name="echo", path=d, enabled=False)
    with _no_entry_points():
        (desc,) = discover_plugins([record])
    assert desc.status == "ok"
    assert desc.description == "Example"
    assert desc.channel_names == ["echo"]
    assert desc.skill_dirs == [(d / "claude-plugin").resolve()]


def test_unimportable_enabled_plugin_is_error_not_fatal(tmp_path):
    d = _write_plugin_dir(
        tmp_path,
        "broken",
        injection_points={
            "channels": [{"name": "broken", "factory": "no_such_module_xyz:make"}]
        },
    )
    with _no_entry_points():
        (desc,) = discover_plugins([PluginRecord(name="broken", path=d, enabled=True)])
    assert desc.status == "error"
    assert "ModuleNotFoundError" in desc.error


def test_disabled_plugin_is_not_imported(tmp_path):
    """A disabled plugin must not execute any of its code during discovery."""
    d = _write_plugin_dir(
        tmp_path,
        "lazy",
        injection_points={
            "channels": [{"name": "lazy", "factory": "no_such_module_xyz:make"}]
        },
    )
    with _no_entry_points():
        (desc,) = discover_plugins([PluginRecord(name="lazy", path=d, enabled=False)])
    assert desc.status == "ok"


def test_python_path_makes_package_importable(tmp_path):
    """The point of python_path: pointing at a folder works with no install."""
    d = _write_plugin_dir(
        tmp_path,
        "vendored",
        python_path="./src",
        injection_points={
            "channels": [{"name": "vendored", "factory": "vendored_mod:make"}]
        },
    )
    src = d / "src"
    src.mkdir()
    (src / "vendored_mod.py").write_text("def make(**kw):\n    return 'ok'\n", encoding="utf-8")

    with _no_entry_points():
        (desc,) = discover_plugins([PluginRecord(name="vendored", path=d, enabled=True)])
    assert desc.status == "ok", desc.error


# ---------------------------------------------------------------- entry points


def _fake_ep(name: str, value: str):
    ep = MagicMock(spec=importlib.metadata.EntryPoint)
    ep.name = name
    ep.value = value
    return ep


def test_entry_point_plugin_discovered():
    def groups(group):
        if group.endswith("chat_plugins"):
            return [_fake_ep("installed", "some_pkg:create_receiver")]
        return []

    with patch("yuki_conductor.plugins._entry_points", side_effect=groups):
        descs = discover_plugins([])
    assert [d.name for d in descs] == ["installed"]
    assert descs[0].source == "entry_point"
    assert descs[0].channel_names == ["installed"]


def test_registry_record_wins_over_entry_point(tmp_path):
    """A name in both places is one plugin, and the file controls enablement."""
    d = _write_plugin_dir(
        tmp_path,
        "dual",
        injection_points={"channels": [{"name": "dual", "factory": "x:y"}]},
    )

    def groups(group):
        if group.endswith("chat_plugins"):
            return [_fake_ep("dual", "other_pkg:create_receiver")]
        return []

    with patch("yuki_conductor.plugins._entry_points", side_effect=groups):
        descs = discover_plugins([PluginRecord(name="dual", path=d, enabled=False)])
    assert len(descs) == 1
    assert descs[0].source == "path"


# ------------------------------------------------------------------- selection


def test_enabled_channels_skips_disabled_and_broken(tmp_path):
    good = _write_plugin_dir(
        tmp_path,
        "good",
        python_path="./src",
        injection_points={"channels": [{"name": "good", "factory": "good_mod:make"}]},
    )
    (good / "src").mkdir()
    (good / "src" / "good_mod.py").write_text("def make(**kw): return 1\n", encoding="utf-8")
    off = _write_plugin_dir(
        tmp_path, "off", injection_points={"channels": [{"name": "off", "factory": "x:y"}]}
    )
    bad = _write_plugin_dir(
        tmp_path,
        "bad",
        injection_points={"channels": [{"name": "bad", "factory": "no_such_mod_abc:make"}]},
    )

    with _no_entry_points():
        descs = discover_plugins(
            [
                PluginRecord(name="good", path=good, enabled=True),
                PluginRecord(name="off", path=off, enabled=False),
                PluginRecord(name="bad", path=bad, enabled=True),
            ]
        )

    assert enabled_channels(descs) == ["good"]


def test_find_channel_locates_provider():
    with _no_entry_points():
        descs = discover_plugins([PluginRecord(name="slack", builtin=True, enabled=True)])
    found = find_channel("slack", descs)
    assert found is not None
    desc, channel = found
    assert desc.name == "slack"
    assert channel.factory == "yuki_conductor.slack_app:create_receiver"
    assert find_channel("nope", descs) is None
