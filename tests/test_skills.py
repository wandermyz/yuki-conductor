"""Tests for skill-plugin injection into spawned Claude Code sessions."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from yuki_conductor import skills
from yuki_conductor.claude_runner import run_claude
from yuki_conductor.config import project_dir


def _mock_popen(stdout="", stderr="", returncode=0):
    mock = MagicMock(spec=subprocess.Popen)
    mock.stdout = iter(stdout.splitlines(keepends=True))
    mock.stderr = iter(stderr.splitlines(keepends=True))
    mock.wait.return_value = returncode
    mock.returncode = returncode
    mock.pid = 99999
    return mock


def test_bundled_cron_plugin_is_valid_plugin_dir():
    bundled = project_dir() / "plugins" / "yuki-conductor"
    assert (bundled / ".claude-plugin" / "plugin.json").is_file()
    assert (bundled / "skills" / "yuki-conductor-cron" / "SKILL.md").is_file()


def test_skill_plugin_dirs_includes_bundled():
    dirs = skills.skill_plugin_dirs()
    bundled = str(project_dir() / "plugins" / "yuki-conductor")
    assert bundled in dirs


def test_skill_plugin_dirs_discovers_entry_points(tmp_path):
    plugin = tmp_path / "extra"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "extra", "version": "0.1.0", "description": "x"})
    )

    ep = MagicMock()
    ep.name = "extra"
    ep.load.return_value = lambda: str(plugin)

    with patch("importlib.metadata.entry_points", return_value=[ep]):
        dirs = skills.skill_plugin_dirs()

    assert str(plugin) in dirs


def test_skill_plugin_dirs_skips_invalid_entry_point(tmp_path):
    ep = MagicMock()
    ep.name = "broken"
    ep.load.return_value = lambda: str(tmp_path / "does-not-exist")

    with patch("importlib.metadata.entry_points", return_value=[ep]):
        dirs = skills.skill_plugin_dirs()

    assert str(tmp_path / "does-not-exist") not in dirs


def test_run_claude_injects_plugin_dirs_and_system_prompt():
    output = json.dumps({"type": "result", "result": "ok", "session_id": "s1"}) + "\n"
    mock_proc = _mock_popen(stdout=output)
    fake_dirs = ["C:/plug/a", "C:/plug/b"]

    with (
        patch("subprocess.Popen", return_value=mock_proc) as mock_cls,
        patch("yuki_conductor.claude_runner.skill_plugin_dirs", return_value=fake_dirs),
    ):
        run_claude("hi")

    cmd = mock_cls.call_args[0][0]
    assert "--append-system-prompt" in cmd
    assert skills.SYSTEM_PROMPT in cmd
    for d in fake_dirs:
        assert d in cmd
    # each dir is preceded by a --plugin-dir flag
    assert cmd.count("--plugin-dir") == len(fake_dirs)


def test_bundled_plugin_dir_helper():
    assert skills._is_plugin_dir(Path(skills._bundled_plugin_dir()))
