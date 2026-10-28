"""CodexHarness: command line and parsing of real `codex exec --json` runs.

The fixtures in tests/fixtures/harness/codex were recorded on 2026-10-27 with
codex-cli 0.163.0 against a LiteLLM proxy: tool_turn (shell calls, one
failing), resume (a resumed session) and bad_model (a model the proxy refused).
"""

import json
import subprocess
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
    return CodexHarness().launch(req)


def _instructions(argv: list[str]) -> str:
    for i, arg in enumerate(argv):
        if arg == "-c" and argv[i + 1].startswith("developer_instructions="):
            return json.loads(argv[i + 1].split("=", 1)[1])
    raise AssertionError("no developer_instructions override")


def test_new_conversation_command_line():
    argv = _launch().argv
    assert argv[:3] == ["codex", "exec", "--json"]
    assert "resume" not in argv
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--skip-git-repo-check" in argv
    # The prompt comes after `--`, so one starting with "-" isn't read as a flag.
    assert argv[-2:] == ["--", "hello"]


def test_resume_puts_the_session_before_the_prompt():
    argv = _launch(session_id="sess-1").argv
    assert argv[:3] == ["codex", "exec", "resume"]
    assert argv[-3:] == ["--", "sess-1", "hello"]


def test_model_is_config_toml_unless_named(monkeypatch):
    assert "-m" not in _launch().argv
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
    assert result.text.startswith("litellm.BadRequestError: You passed in model=no-such-model")
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


def _rollout(home: Path, session_id: str, *infos) -> None:
    day = home / "sessions" / "2026" / "10" / "27"
    day.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": info}})
             for info in infos]
    (day / f"rollout-2026-10-27T17-00-00-{session_id}.jsonl").write_text("\n".join(lines) + "\n")


def test_context_is_the_last_call_from_the_rollout(codex_home):
    _rollout(
        codex_home, TOOL_TURN_ID,
        {"last_token_usage": {"input_tokens": 100}, "model_context_window": 258400},
        {"last_token_usage": {"input_tokens": 15210, "cached_input_tokens": 14600},
         "total_token_usage": {"input_tokens": 29590}, "model_context_window": 258400},
    )
    _, result = _parse("tool_turn")
    assert result.context_tokens == 15210
    assert result.context_window == 258400


def test_context_is_unknown_without_a_rollout():
    _, result = _parse("tool_turn")
    assert result.context_tokens is None and result.context_window is None


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


def test_run_agent_streams_events_with_stdin_closed():
    seen = []
    proc = _mock_popen("".join(_lines("tool_turn")))
    with patch("subprocess.Popen", return_value=proc) as popen:
        result = run_agent(KNOWN_HARNESSES["codex"], "hi", cwd="/work", on_event=seen.append)
    assert result.session_id == TOOL_TURN_ID
    assert [e.kind for e in seen][:3] == ["init", "text", "tool_use"]
    # Codex reads an open stdin as more prompt and would wait on it forever.
    assert popen.call_args.kwargs["stdin"] is subprocess.DEVNULL


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


def _catalog(monkeypatch, stdout: str):
    monkeypatch.setattr(
        codex_mod.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=""),
    )


def test_models_come_from_the_catalog_without_hidden_ones(codex_home, monkeypatch):
    (codex_home / "config.toml").write_text('model = "gpt-6-sol"\n')
    _catalog(monkeypatch, json.dumps({"models": [
        {"slug": "gpt-6-sol", "display_name": "GPT-6-Sol", "visibility": "list"},
        {"slug": "internal", "display_name": "Internal", "visibility": "hide"},
        {"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list"},
    ]}))
    codex = CodexHarness()
    assert [(m.id, m.label) for m in codex.models()] == [
        ("gpt-6-sol", "GPT-6-Sol"), ("gpt-5.5", "GPT-5.5"),
    ]
    assert codex.default_model() == "gpt-6-sol"


def test_a_configured_model_missing_from_the_catalog_is_still_offered(codex_home, monkeypatch):
    (codex_home / "config.toml").write_text('model = "proxy-alias"\n')
    _catalog(monkeypatch, json.dumps({"models": [{"slug": "gpt-5.5", "visibility": "list"}]}))
    assert [m.id for m in CodexHarness().models()] == ["proxy-alias", "gpt-5.5"]


def test_an_unreadable_catalog_offers_only_the_default(codex_home, monkeypatch):
    _catalog(monkeypatch, "not json")
    assert CodexHarness().models() == []
    monkeypatch.setattr(codex_mod, "CODEX_DEFAULT_MODEL", "gpt-6-luna")
    assert [m.id for m in CodexHarness().models()] == ["gpt-6-luna"]
