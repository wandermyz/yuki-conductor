"""CodexHarness: command line and parsing of real `codex exec --json` runs.

The fixtures in tests/fixtures/harness/codex were recorded on 2026-10-27 with
codex-cli 0.163.0 against an OpenAI-compatible endpoint: tool_turn (shell
calls, one failing), resume (a resumed session) and bad_model (a model the
provider refused; its error body rewritten to a generic one).
"""

import json
import subprocess
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from yuki_conductor import skills
from yuki_conductor.harness import KNOWN_HARNESSES
from yuki_conductor.harness import codex as codex_mod
from yuki_conductor.harness.base import RunRequest
from yuki_conductor.harness.codex import CodexHarness, CodexStreamParser
from yuki_conductor.harness.process import run_agent

_resolve_bin = codex_mod.resolve_bin
FIXTURES = Path(__file__).parent / "fixtures" / "harness" / "codex"
TOOL_TURN_ID = "01a17fd7-b95b-7d20-b777-ab4c701e6264"


@pytest.fixture(autouse=True)
def codex_home(tmp_path, monkeypatch):
    """An empty Codex home, so no test reads the real session store."""
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    # Don't depend on how codex is installed on the machine running the tests.
    monkeypatch.setattr(codex_mod, "resolve_bin", lambda name: name)
    return home


def _lines(name: str) -> list[str]:
    return (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)


def _parse(name: str):
    parser = CodexStreamParser()
    events = [e for line in _lines(name) for e in parser.feed(line)]
    return events, parser.finish("".join(_lines(name)), fallback_session_id="fallback")


def test_registered():
    assert isinstance(KNOWN_HARNESSES["codex"], CodexHarness)


# --------------------------------------------------------------------------
# launch
# --------------------------------------------------------------------------


def _launch(**kw):
    req = RunRequest(prompt="hello", session_id=None, model=None, cwd="/work",
                     conversation_key=None)
    for k, v in kw.items():
        setattr(req, k, v)
    launch = CodexHarness().launch(req)
    _launches.append(launch)
    return launch


_launches = []


@pytest.fixture(autouse=True)
def cleanup_launches():
    yield
    for launch in _launches:
        for path in launch.temp_files:
            Path(path).unlink(missing_ok=True)
    _launches.clear()


def _instructions(argv: list[str]) -> str:
    profile = argv[argv.index("--profile") + 1]
    path = codex_mod._codex_home() / f"{profile}.config.toml"
    return tomllib.loads(path.read_text(encoding="utf-8"))["developer_instructions"]


def test_new_conversation_command_line():
    argv = _launch().argv
    assert argv[:2] == ["codex", "exec"]
    assert "--json" in argv
    assert "resume" not in argv
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--skip-git-repo-check" in argv
    # The prompt comes after `--`, so one starting with "-" isn't read as a flag.
    assert argv[-2:] == ["--", "-"]


def test_resume_puts_the_session_before_the_prompt():
    argv = _launch(session_id="sess-1").argv
    assert argv[:2] == ["codex", "exec"]
    assert argv.index("--profile") < argv.index("resume")
    assert argv[-3:] == ["--", "sess-1", "-"]


def test_model_falls_back_to_the_sol_alias_unless_named(monkeypatch):
    argv = _launch().argv
    assert argv[argv.index("-m") + 1] == "gpt-6.1-sol"
    argv = _launch(model="gpt-6-sol").argv
    assert argv[argv.index("-m") + 1] == "gpt-6-sol"
    monkeypatch.setattr(codex_mod, "CODEX_DEFAULT_MODEL", "gpt-6-luna")
    argv = _launch().argv
    assert argv[argv.index("-m") + 1] == "gpt-6-luna"


def test_instructions_carry_the_contract_and_yuki_skills():
    text = _instructions(_launch(conversation_key="conv-9", task_id=5).argv)
    assert "<attachment>" in text
    assert "conv-9" in text
    assert "task #5" in text
    # The bundled plugin's skills, by path, since Codex can't load the plugin.
    assert "`yuki-conductor-send`" in text
    assert str(Path("yuki-conductor-send") / "SKILL.md") in text


def test_instructions_skip_user_skills(tmp_path, monkeypatch):
    user = tmp_path / "user-skills" / "mine"
    user.mkdir(parents=True)
    (user / "SKILL.md").write_text("---\nname: mine\ndescription: x\n---\n")
    monkeypatch.setattr(skills, "USER_SKILLS_DIR", tmp_path / "user-skills")
    assert "`mine`" not in skills.codex_prompt([], cwd=tmp_path)


def test_resolve_bin_prefers_the_vendored_exe_over_the_npm_shim(tmp_path, monkeypatch):
    shim = tmp_path / "codex.cmd"
    shim.write_text("")
    exe = (tmp_path / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai"
           / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe")
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(codex_mod.shutil, "which", lambda name: str(shim))
    assert _resolve_bin("codex") == str(exe)
    monkeypatch.setattr(codex_mod.shutil, "which", lambda name: "/usr/bin/codex")
    assert _resolve_bin("codex") == "/usr/bin/codex"


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def test_tool_turn():
    events, result = _parse("tool_turn")
    assert [e.kind for e in events] == [
        "init", "text",
        "tool_use", "tool_result",
        "tool_use", "tool_result",
        "tool_use", "tool_result",
        "text",
    ]
    assert events[2].label.startswith("Shell(")
    assert events[2].detail["tool_use_id"] == events[3].detail["tool_use_id"] == "item_1"
    failed = events[7]
    assert failed.detail["is_error"] and failed.label == "error: exit code 3"
    assert not result.is_error
    assert result.session_id == TOOL_TURN_ID
    # The last message is the answer, not the narration before the tools.
    assert result.text.startswith("Created `notes.txt`")
    assert result.input_tokens == 29590 and result.output_tokens == 210


def test_resume_keeps_the_session():
    _, result = _parse("resume")
    assert result.session_id == TOOL_TURN_ID
    assert "notes.txt" in result.text


def test_failed_turn_reads_the_provider_message():
    events, result = _parse("bad_model")
    assert result.is_error
    assert result.text == (
        "The model `no-such-model` does not exist or you do not have access to it."
    )
    # Codex's own warning item is shown as a step, not taken as the reply.
    assert events[1].kind == "status"


def test_garbage_lines_are_ignored():
    parser = CodexStreamParser()
    assert parser.feed("not json\n") == []
    assert parser.feed("[1]\n") == []
    result = parser.finish("not json", "s")
    assert result.is_error and result.session_id == "s"


def test_reasoning_and_file_changes_become_steps():
    parser = CodexStreamParser()
    events = parser.feed(json.dumps({"type": "item.completed", "item": {
        "id": "i1", "type": "reasoning", "text": "Considering the options"}}))
    events += parser.feed(json.dumps({"type": "item.completed", "item": {
        "id": "i2", "type": "file_change",
        "changes": [{"path": "a.py", "kind": "update"}, {"path": "b.py", "kind": "add"}]}}))
    assert [e.kind for e in events] == ["thinking", "tool_use"]
    assert events[1].label == "Edit(a.py, b.py)"


# --------------------------------------------------------------------------
# context window occupation
# --------------------------------------------------------------------------


def _rollout(home: Path, session_id: str, *infos, model: str | None = None) -> None:
    day = home / "sessions" / "2026" / "10" / "27"
    day.mkdir(parents=True, exist_ok=True)
    lines = []
    if model:
        lines.append(json.dumps({"type": "turn_context", "payload": {"model": model}}))
    lines += [json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": info}})
              for info in infos]
    (day / f"rollout-2026-10-27T17-00-00-{session_id}.jsonl").write_text("\n".join(lines) + "\n")


def test_context_is_the_last_call_from_the_rollout(codex_home):
    _rollout(
        codex_home, TOOL_TURN_ID,
        {"last_token_usage": {"input_tokens": 100}, "model_context_window": 258400},
        {"last_token_usage": {"input_tokens": 15210, "cached_input_tokens": 14600},
         "total_token_usage": {"input_tokens": 29590}, "model_context_window": 258400},
        model="gpt-6.1-sol",
    )
    _, result = _parse("tool_turn")
    assert result.context_tokens == 15210
    assert result.context_window == 258400
    assert result.model == "gpt-6.1-sol"


def test_context_is_unknown_without_a_rollout():
    _, result = _parse("tool_turn")
    assert result.context_tokens is None and result.context_window is None
    assert result.model is None


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------


def test_session_exists_searches_the_rollouts(codex_home):
    codex = CodexHarness()
    assert not codex.session_exists("sess-1", "/work")
    _rollout(codex_home, "sess-1", {})
    assert codex.session_exists("sess-1", "/work")
    assert not codex.session_exists("sess-2", "/work")


# --------------------------------------------------------------------------
# end to end through run_agent
# --------------------------------------------------------------------------


def _mock_popen(stdout: str, returncode: int = 0, stderr: str = ""):
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = iter(stdout.splitlines(keepends=True))
    proc.stderr = iter(stderr.splitlines(keepends=True))
    proc.wait.return_value = returncode
    proc.returncode = returncode
    proc.pid = 99999
    return proc


def test_run_agent_streams_events_with_finite_prompt_input():
    seen = []
    proc = _mock_popen("".join(_lines("tool_turn")))
    with patch("subprocess.Popen", return_value=proc) as popen:
        result = run_agent(KNOWN_HARNESSES["codex"], "hi", cwd="/work", on_event=seen.append)
    assert result.session_id == TOOL_TURN_ID
    assert [e.kind for e in seen][:3] == ["init", "text", "tool_use"]
    stdin = popen.call_args.kwargs["stdin"]
    assert stdin.closed
    assert not Path(stdin.name).exists()


def test_run_agent_reports_the_failed_turn_not_the_exit_code():
    proc = _mock_popen("".join(_lines("bad_model")), returncode=1,
                       stderr="Reading additional input from stdin...\n")
    with patch("subprocess.Popen", return_value=proc):
        result = run_agent(KNOWN_HARNESSES["codex"], "hi", cwd="/work")
    assert result.is_error
    assert "no-such-model" in result.text


def test_run_agent_flags_a_missing_session():
    proc = _mock_popen("", returncode=1, stderr=(
        "Error: thread/resume: thread/resume failed: no rollout found for thread id "
        "sess-gone (code -32600)\n"
    ))
    with patch("subprocess.Popen", return_value=proc):
        result = run_agent(KNOWN_HARNESSES["codex"], "hi", session_id="sess-gone", cwd="/work")
    assert result.is_error and result.session_missing


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------


def test_models_are_the_static_aliases(codex_home):
    codex = CodexHarness()
    assert [(m.id, m.label) for m in codex.models()] == [
        ("gpt-6-astra", "astra"), ("gpt-6.1-sol", "sol"),
    ]
    assert codex.default_model() == "gpt-6.1-sol"


def test_codex_default_model_env_overrides_the_alias(codex_home, monkeypatch):
    monkeypatch.setattr(codex_mod, "CODEX_DEFAULT_MODEL", "gpt-6-luna")
    assert CodexHarness().default_model() == "gpt-6-luna"
    argv = _launch().argv
    assert argv[argv.index("-m") + 1] == "gpt-6-luna"


def test_picker_labels_the_default_by_its_alias(monkeypatch):
    assert CodexHarness().default_label() == "sol"
    monkeypatch.setattr(codex_mod, "CODEX_DEFAULT_MODEL", "gpt-6-luna")
    assert CodexHarness().default_label() == "gpt-6-luna"


@pytest.mark.parametrize("session_id", [None, "sess-1"])
def test_large_instructions_and_prompt_stay_off_command_line(monkeypatch, session_id):
    instructions = '\u6280\u80fd "quoted" \n' * 10000
    prompt = '-task "quoted" \n' * 10000
    monkeypatch.setattr(codex_mod, "codex_prompt", lambda *a, **kw: instructions)
    launch = _launch(prompt=prompt, session_id=session_id)
    assert len(subprocess.list2cmdline(launch.argv).encode("utf-16-le")) < 4000
    assert _instructions(launch.argv) == instructions
    assert Path(launch.stdin_file).read_text(encoding="utf-8") == prompt
    assert launch.env["CODEX_HOME"] == str(codex_mod._codex_home())


@pytest.mark.parametrize("failed_start", [False, True])
def test_run_agent_cleans_profile_and_prompt(monkeypatch, failed_start):
    launch = _launch()
    monkeypatch.setattr(CodexHarness, "launch", lambda *a: launch)
    proc = _mock_popen("".join(_lines("tool_turn")))

    def start(*args, **kwargs):
        assert kwargs["stdin"].read() == "hello"
        assert all(Path(path).exists() for path in launch.temp_files)
        if failed_start:
            raise FileNotFoundError("missing executable")
        return proc

    with patch("subprocess.Popen", side_effect=start):
        result = run_agent(CodexHarness(), "ignored", cwd="/work")
    assert result.is_error == failed_start
    assert all(not Path(path).exists() for path in launch.temp_files)
