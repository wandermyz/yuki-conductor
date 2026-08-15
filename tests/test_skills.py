"""Tests for skill discovery and injection into spawned Claude Code sessions."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from yuki_conductor import skills
from yuki_conductor.claude_runner import run_claude
from yuki_conductor.config import project_dir
from yuki_conductor.skills import Tier


def _mock_popen(stdout="", stderr="", returncode=0):
    mock = MagicMock(spec=subprocess.Popen)
    mock.stdout = iter(stdout.splitlines(keepends=True))
    mock.stderr = iter(stderr.splitlines(keepends=True))
    mock.wait.return_value = returncode
    mock.returncode = returncode
    mock.pid = 99999
    return mock


def _write_skill(root: Path, name: str, description: str = "Does a thing") -> Path:
    """Create <root>/<name>/SKILL.md with minimal frontmatter."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody\n", encoding="utf-8"
    )
    return skill_dir


def _isolated(tmp_path, **overrides):
    """Patch every filesystem source skills.py reads, so tests see only tmp_path."""
    defaults = {
        "USER_SKILLS_DIR": tmp_path / "no-user-skills",
        "SKILLS_CONFIG_FILE": tmp_path / "no-skills.yaml",
        "SYSTEM_PROMPT_FILE": tmp_path / "no-prompt.md",
    }
    defaults.update(overrides)
    return [patch.object(skills, key, value) for key, value in defaults.items()]


def _with(patches):
    for p in patches:
        p.start()
    return patches


def _stop(patches):
    for p in patches:
        p.stop()


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------


def test_bundled_plugin_provides_only_global_skills():
    """The bundled plugin is injected everywhere, so it must hold only globals."""
    bundled = project_dir() / "plugins" / "yuki-conductor"
    assert (bundled / ".claude-plugin" / "plugin.json").is_file()
    assert (bundled / "skills" / "yuki-conductor-cron" / "SKILL.md").is_file()
    # restart is project-scoped and must NOT ship in the global plugin
    assert not (bundled / "skills" / "yuki-conductor-restart").exists()


def test_restart_skill_is_project_scoped():
    skill_dir = project_dir() / ".claude" / "skills" / "yuki-conductor-restart"
    assert (skill_dir / "SKILL.md").is_file()
    assert (skill_dir / "restart-daemon.ps1").is_file()


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


def test_bundled_plugin_dir_helper():
    assert skills._is_plugin_dir(Path(skills._bundled_plugin_dir()))


# --------------------------------------------------------------------------
# Tier resolution
# --------------------------------------------------------------------------


def test_resolve_tags_plugin_skills_as_yuki(tmp_path):
    _write_skill(tmp_path / "plug" / "skills", "global-thing")
    patches = _with(_isolated(tmp_path))
    try:
        resolved = skills.resolve_skills(cwd=None, plugin_dirs=[str(tmp_path / "plug")])
    finally:
        _stop(patches)

    assert [(s.name, s.tier) for s in resolved] == [("global-thing", Tier.YUKI)]


def test_resolve_tags_cwd_skills_as_project(tmp_path):
    proj = tmp_path / "proj"
    _write_skill(proj / ".claude" / "skills", "proj-thing")
    patches = _with(_isolated(tmp_path))
    try:
        resolved = skills.resolve_skills(cwd=proj, plugin_dirs=[])
    finally:
        _stop(patches)

    assert [(s.name, s.tier) for s in resolved] == [("proj-thing", Tier.PROJECT)]


def test_project_skills_absent_from_other_cwd(tmp_path):
    proj = tmp_path / "proj"
    _write_skill(proj / ".claude" / "skills", "proj-thing")
    other = tmp_path / "other"
    other.mkdir()

    patches = _with(_isolated(tmp_path))
    try:
        resolved = skills.resolve_skills(cwd=other, plugin_dirs=[])
    finally:
        _stop(patches)

    assert resolved == []


def test_user_skills_are_listed(tmp_path):
    user = tmp_path / "user-skills"
    _write_skill(user, "ambient-thing")

    patches = _with(_isolated(tmp_path, USER_SKILLS_DIR=user))
    try:
        resolved = skills.resolve_skills(cwd=None, plugin_dirs=[])
    finally:
        _stop(patches)

    assert [(s.name, s.tier) for s in resolved] == [("ambient-thing", Tier.USER)]


def test_always_config_promotes_user_skill(tmp_path):
    user = tmp_path / "user-skills"
    _write_skill(user, "promoted")
    _write_skill(user, "ambient")
    config = tmp_path / "skills.yaml"
    config.write_text("always:\n  - promoted\n", encoding="utf-8")

    patches = _with(_isolated(tmp_path, USER_SKILLS_DIR=user, SKILLS_CONFIG_FILE=config))
    try:
        resolved = skills.resolve_skills(cwd=None, plugin_dirs=[])
    finally:
        _stop(patches)

    tiers = {s.name: s.tier for s in resolved}
    assert tiers == {"promoted": Tier.ALWAYS, "ambient": Tier.USER}


def test_always_config_missing_skill_warns(tmp_path, caplog):
    user = tmp_path / "user-skills"
    user.mkdir()
    config = tmp_path / "skills.yaml"
    config.write_text("always:\n  - typo-name\n", encoding="utf-8")

    patches = _with(_isolated(tmp_path, USER_SKILLS_DIR=user, SKILLS_CONFIG_FILE=config))
    try:
        with caplog.at_level("WARNING"):
            skills.resolve_skills(cwd=None, plugin_dirs=[])
    finally:
        _stop(patches)

    assert "typo-name" in caplog.text


def test_more_specific_tier_shadows_user_skill(tmp_path):
    proj = tmp_path / "proj"
    _write_skill(proj / ".claude" / "skills", "dupe", description="project version")
    user = tmp_path / "user-skills"
    _write_skill(user, "dupe", description="user version")

    patches = _with(_isolated(tmp_path, USER_SKILLS_DIR=user))
    try:
        resolved = skills.resolve_skills(cwd=proj, plugin_dirs=[])
    finally:
        _stop(patches)

    assert len(resolved) == 1
    assert resolved[0].tier is Tier.PROJECT
    assert resolved[0].description == "project version"


def test_resolve_handles_missing_dirs(tmp_path):
    patches = _with(_isolated(tmp_path))
    try:
        assert skills.resolve_skills(cwd=tmp_path / "nope", plugin_dirs=[]) == []
    finally:
        _stop(patches)


# --------------------------------------------------------------------------
# Prompt rendering
# --------------------------------------------------------------------------


def test_prompt_groups_skills_under_tier_headings(tmp_path):
    _write_skill(tmp_path / "plug" / "skills", "cron-like")
    proj = tmp_path / "proj"
    _write_skill(proj / ".claude" / "skills", "repo-only")
    user = tmp_path / "user-skills"
    _write_skill(user, "ambient")

    patches = _with(_isolated(tmp_path, USER_SKILLS_DIR=user))
    try:
        prompt = skills.system_prompt([str(tmp_path / "plug")], cwd=proj)
    finally:
        _stop(patches)

    assert "yuki-conductor capabilities" in prompt
    assert "This project" in prompt
    assert "Your other skills" in prompt
    # each skill appears under its own heading, in tier order
    assert prompt.index("cron-like") < prompt.index("repo-only") < prompt.index("ambient")


def test_prompt_omits_empty_tiers(tmp_path):
    _write_skill(tmp_path / "plug" / "skills", "cron-like")

    patches = _with(_isolated(tmp_path))
    try:
        prompt = skills.system_prompt([str(tmp_path / "plug")], cwd=None)
    finally:
        _stop(patches)

    assert "This project" not in prompt
    assert "Your other skills" not in prompt


def test_prompt_instructs_skill_tool_invocation(tmp_path):
    patches = _with(_isolated(tmp_path))
    try:
        prompt = skills.system_prompt([], cwd=None)
    finally:
        _stop(patches)

    assert "Skill" in prompt
    assert "no automatic skill listing" in prompt


def test_prompt_names_real_bundled_cron_skill(tmp_path):
    patches = _with(_isolated(tmp_path))
    try:
        prompt = skills.system_prompt(
            [str(project_dir() / "plugins" / "yuki-conductor")], cwd=None
        )
    finally:
        _stop(patches)

    assert "`yuki-conductor-cron`" in prompt
    # description comes along so the model can route on it
    assert "schedule" in prompt.lower()


def test_prompt_includes_restart_skill_only_in_project(tmp_path):
    plugin_dirs = [str(project_dir() / "plugins" / "yuki-conductor")]

    patches = _with(_isolated(tmp_path))
    try:
        in_project = skills.system_prompt(plugin_dirs, cwd=project_dir())
        elsewhere = skills.system_prompt(plugin_dirs, cwd=tmp_path)
    finally:
        _stop(patches)

    assert "yuki-conductor-restart" in in_project
    assert "yuki-conductor-restart" not in elsewhere
    # cron is global, so it shows up either way
    assert "yuki-conductor-cron" in in_project
    assert "yuki-conductor-cron" in elsewhere


def test_prompt_appends_workspace_file(tmp_path):
    extra = tmp_path / "system-prompt.md"
    extra.write_text("Use the frobnicator for all frobbing.\n", encoding="utf-8")

    patches = _with(_isolated(tmp_path, SYSTEM_PROMPT_FILE=extra))
    try:
        prompt = skills.system_prompt([], cwd=None)
    finally:
        _stop(patches)

    assert prompt.endswith("Use the frobnicator for all frobbing.")


def test_prompt_ignores_empty_workspace_file(tmp_path):
    extra = tmp_path / "system-prompt.md"
    extra.write_text("   \n", encoding="utf-8")

    patches = _with(_isolated(tmp_path, SYSTEM_PROMPT_FILE=extra))
    try:
        with_file = skills.system_prompt([], cwd=None)
    finally:
        _stop(patches)

    patches = _with(_isolated(tmp_path))
    try:
        without_file = skills.system_prompt([], cwd=None)
    finally:
        _stop(patches)

    assert with_file == without_file


# --------------------------------------------------------------------------
# Frontmatter parsing
# --------------------------------------------------------------------------


def test_reads_folded_description(tmp_path):
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: >\n  First line.\n  Second line.\n---\n\nBody\n",
        encoding="utf-8",
    )

    found = skills._scan_skill_dir(tmp_path, Tier.USER)
    assert [(s.name, s.description) for s in found] == [
        ("demo-skill", "First line. Second line.")
    ]


def test_reads_plain_scalar_description(tmp_path):
    skill_dir = tmp_path / "plain"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        '---\nname: plain\ndescription: "Does a thing"\nallowed-tools: Bash(ls *)\n---\n',
        encoding="utf-8",
    )

    found = skills._scan_skill_dir(tmp_path, Tier.USER)
    assert [(s.name, s.description) for s in found] == [("plain", "Does a thing")]


def test_falls_back_to_dir_name(tmp_path):
    skill_dir = tmp_path / "nameless"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("No frontmatter here\n", encoding="utf-8")

    found = skills._scan_skill_dir(tmp_path, Tier.USER)
    assert [(s.name, s.description) for s in found] == [("nameless", "")]


# --------------------------------------------------------------------------
# run_claude wiring
# --------------------------------------------------------------------------


def test_run_claude_injects_plugin_dirs_and_system_prompt():
    output = json.dumps({"type": "result", "result": "ok", "session_id": "s1"}) + "\n"
    mock_proc = _mock_popen(stdout=output)
    fake_dirs = ["C:/plug/a", "C:/plug/b"]

    with (
        patch("subprocess.Popen", return_value=mock_proc) as mock_cls,
        patch("yuki_conductor.claude_runner.skill_plugin_dirs", return_value=fake_dirs),
    ):
        run_claude("hi", cwd="C:/work")

    cmd = mock_cls.call_args[0][0]
    assert "--append-system-prompt" in cmd
    assert skills.system_prompt(fake_dirs, cwd="C:/work") in cmd
    for d in fake_dirs:
        assert d in cmd
    assert cmd.count("--plugin-dir") == len(fake_dirs)


def test_run_claude_passes_session_cwd_to_system_prompt():
    """Project-scope skills depend on the prompt being built for the real cwd."""
    output = json.dumps({"type": "result", "result": "ok", "session_id": "s1"}) + "\n"
    mock_proc = _mock_popen(stdout=output)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("yuki_conductor.claude_runner.skill_plugin_dirs", return_value=[]),
        patch("yuki_conductor.claude_runner.system_prompt", return_value="p") as mock_prompt,
    ):
        run_claude("hi", cwd="C:/some/project")

    assert mock_prompt.call_args.kwargs["cwd"] == "C:/some/project"


def test_run_claude_enables_all_setting_sources():
    output = json.dumps({"type": "result", "result": "ok", "session_id": "s1"}) + "\n"
    mock_proc = _mock_popen(stdout=output)

    with (
        patch("subprocess.Popen", return_value=mock_proc) as mock_cls,
        patch("yuki_conductor.claude_runner.skill_plugin_dirs", return_value=[]),
    ):
        run_claude("hi")

    cmd = mock_cls.call_args[0][0]
    assert "--setting-sources" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == "user,project,local"
