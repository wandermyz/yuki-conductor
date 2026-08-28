"""Tests for claude_runner module."""

import json
import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

from yuki_conductor.claude_runner import run_claude


def _stream(*records: dict) -> str:
    """Build NDJSON stdout as the CLI's stream-json format emits it."""
    return "".join(json.dumps(r) + "\n" for r in records)


def _result_record(result="ok", session_id="s", **extra) -> dict:
    return {"type": "result", "subtype": "success", "result": result,
            "session_id": session_id, **extra}


def _mock_popen(stdout="", stderr="", returncode=0):
    """Create a mock Popen whose stdout/stderr iterate like line-buffered pipes."""
    mock = MagicMock(spec=subprocess.Popen)
    mock.stdout = iter(stdout.splitlines(keepends=True))
    mock.stderr = iter(stderr.splitlines(keepends=True))
    mock.communicate.return_value = (stdout, stderr)
    mock.wait.return_value = returncode
    mock.returncode = returncode
    mock.pid = 99999
    return mock


def test_basic_call():
    output = _stream(_result_record("Hello!", "sess_123"))
    mock_proc = _mock_popen(stdout=output)
    with patch("subprocess.Popen", return_value=mock_proc) as mock_cls:
        result = run_claude("hi")

    assert result.text == "Hello!"
    assert result.session_id == "sess_123"
    assert not result.is_error

    cmd = mock_cls.call_args[0][0]
    cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
    assert "claude" in cmd_str
    assert "-p" in cmd_str
    assert "--dangerously-skip-permissions" in cmd_str
    assert "--output-format" in cmd_str
    assert "stream-json" in cmd_str
    assert "--verbose" in cmd_str
    assert "hi" in cmd_str


def test_intermediate_steps_are_streamed_to_callback():
    """Tool calls arrive as events before the final result is returned."""
    output = _stream(
        {"type": "system", "subtype": "init", "model": "opus", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Let me look."},
            {"type": "tool_use", "id": "t1", "name": "Read",
             "input": {"file_path": "/tmp/a.py"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "file body"},
        ]}},
        _result_record("done", "s1"),
    )
    seen = []
    mock_proc = _mock_popen(stdout=output)
    with patch("subprocess.Popen", return_value=mock_proc):
        result = run_claude("go", on_event=seen.append)

    assert result.text == "done"
    assert [e.kind for e in seen] == ["init", "text", "tool_use", "tool_result"]
    assert seen[2].label == "Read(/tmp/a.py)"
    assert seen[3].detail["tool_use_id"] == "t1"


def test_callback_exception_does_not_abort_run():
    output = _stream(
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
        ]}},
        _result_record("finished", "s1"),
    )

    def boom(_event):
        raise RuntimeError("subscriber died")

    mock_proc = _mock_popen(stdout=output)
    with patch("subprocess.Popen", return_value=mock_proc):
        result = run_claude("go", on_event=boom)

    assert result.text == "finished"
    assert not result.is_error


def test_usage_and_cost_from_result_record():
    output = _stream(
        _result_record(
            "ok", "s1",
            total_cost_usd=0.42,
            usage={"input_tokens": 120, "output_tokens": 35},
        )
    )
    mock_proc = _mock_popen(stdout=output)
    with patch("subprocess.Popen", return_value=mock_proc):
        result = run_claude("hi")

    assert result.input_tokens == 120
    assert result.output_tokens == 35
    assert result.cost_usd == 0.42


def test_no_model_flag_when_unset():
    """A channel with no pinned model must not get --model.

    Omitting the flag is what lets the claude CLI apply its own default
    (opus[1m]); passing an alias like "opus" resolves to the plain 200k
    variant instead, silently giving up the 1M context window.
    """
    mock_proc = _mock_popen(stdout=_stream(_result_record()))
    with patch("subprocess.Popen", return_value=mock_proc) as mock_cls:
        run_claude("hi", model=None)

    cmd = mock_cls.call_args[0][0]
    assert "--model" not in cmd


def test_model_flag_when_pinned():
    """Bracketed values must reach the CLI intact — they select the 1M variants."""
    mock_proc = _mock_popen(stdout=_stream(_result_record()))
    with patch("subprocess.Popen", return_value=mock_proc) as mock_cls:
        run_claude("hi", model="opus[1m]")

    cmd = mock_cls.call_args[0][0]
    assert cmd[cmd.index("--model") + 1] == "opus[1m]"


def test_resume_session():
    output = _stream(_result_record("Resumed!", "sess_456"))
    mock_proc = _mock_popen(stdout=output)
    with patch("subprocess.Popen", return_value=mock_proc) as mock_cls:
        result = run_claude("continue", session_id="sess_456")

    cmd = mock_cls.call_args[0][0]
    cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
    assert "-r" in cmd_str
    assert "sess_456" in cmd_str
    assert result.text == "Resumed!"


def test_timeout():
    """A silent CLI is killed by the idle watchdog and reported as a timeout."""
    killed = threading.Event()

    def hanging_stdout():
        # Emulate a pipe that yields nothing until the process is killed.
        killed.wait(5)
        return
        yield  # pragma: no cover — makes this a generator

    mock_proc = _mock_popen()
    mock_proc.stdout = hanging_stdout()

    with patch("subprocess.Popen", return_value=mock_proc), \
         patch("yuki_conductor.claude_runner._kill_tree", side_effect=lambda p: killed.set()) as kill:
        result = run_claude("slow prompt", timeout=0.05)

    assert result.is_error
    assert "no output" in result.text
    kill.assert_called_once()


def test_slow_but_chatty_run_is_not_killed():
    """The deadline is idle-based: steady output past the timeout must survive."""
    records = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": f"step {i}"}]}}
        for i in range(6)
    ]
    lines = _stream(*records, _result_record("Done!", "sess_slow")).splitlines(keepends=True)

    def trickling_stdout():
        for line in lines:
            time.sleep(0.05)
            yield line

    mock_proc = _mock_popen()
    mock_proc.stdout = trickling_stdout()

    with patch("subprocess.Popen", return_value=mock_proc), \
         patch("yuki_conductor.claude_runner._kill_tree") as kill:
        # Total runtime (~0.35s) far exceeds the timeout; no single gap does.
        result = run_claude("slow prompt", timeout=0.2)

    assert not result.is_error
    assert result.text == "Done!"
    kill.assert_not_called()


def test_cli_not_found():
    with patch("subprocess.Popen", side_effect=FileNotFoundError):
        result = run_claude("hello")

    assert result.is_error
    assert "not found" in result.text


def test_nonzero_exit():
    mock_proc = _mock_popen(returncode=1, stderr="Some error")
    with patch("subprocess.Popen", return_value=mock_proc):
        result = run_claude("bad prompt")

    assert result.is_error
    assert "Some error" in result.text


def test_no_truncation():
    """The runner returns the full text; length limits belong to each platform."""
    long_text = "x" * 5000
    mock_proc = _mock_popen(stdout=_stream(_result_record(long_text, "s1")))
    with patch("subprocess.Popen", return_value=mock_proc):
        result = run_claude("big")

    assert result.text == long_text


def test_unset_claudecode_env():
    """Verify CLAUDECODE is removed from subprocess env."""
    import os

    original = os.environ.get("CLAUDECODE")
    os.environ["CLAUDECODE"] = "test_value"
    try:
        mock_proc = _mock_popen(stdout=_stream(_result_record()))
        with patch("subprocess.Popen", return_value=mock_proc) as mock_cls:
            run_claude("test")

        env = mock_cls.call_args[1]["env"]
        assert "CLAUDECODE" not in env
    finally:
        if original is None:
            os.environ.pop("CLAUDECODE", None)
        else:
            os.environ["CLAUDECODE"] = original


def test_non_json_output():
    """Garbage on stdout with no result record still surfaces as the reply."""
    mock_proc = _mock_popen(stdout="plain text response\n")
    with patch("subprocess.Popen", return_value=mock_proc):
        result = run_claude("test")

    assert result.text == "plain text response"
    assert not result.is_error


def test_cancel_returns_stopped_message():
    """A cancelled process returns a non-error stopped message."""
    from yuki_conductor.claude_runner import cancel_process

    mock_proc = _mock_popen(returncode=1)
    with patch("subprocess.Popen", return_value=mock_proc), \
         patch("yuki_conductor.claude_runner._kill_tree"):
        original_wait = mock_proc.wait

        def wait_with_cancel(*a, **kw):
            # Simulate: while waiting, someone calls cancel_process
            cancel_process("conv_cancel_test")
            return original_wait(*a, **kw)

        mock_proc.wait = wait_with_cancel
        result = run_claude("test", conversation_key="conv_cancel_test")

    assert not result.is_error
    assert "stopped" in result.text
