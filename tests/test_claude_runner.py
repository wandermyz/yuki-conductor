"""Tests for claude_runner module."""

import json
import subprocess
from unittest.mock import MagicMock, patch

from yuki_conductor.claude_runner import run_claude


def _mock_popen(stdout="", stderr="", returncode=0):
    """Create a mock Popen that returns given stdout/stderr on communicate()."""
    mock = MagicMock(spec=subprocess.Popen)
    mock.communicate.return_value = (stdout, stderr)
    mock.returncode = returncode
    mock.pid = 99999
    return mock


def test_basic_call():
    output = json.dumps({"result": "Hello!", "session_id": "sess_123"})
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
    assert "json" in cmd_str
    assert "hi" in cmd_str


def test_resume_session():
    output = json.dumps({"result": "Resumed!", "session_id": "sess_456"})
    mock_proc = _mock_popen(stdout=output)
    with patch("subprocess.Popen", return_value=mock_proc) as mock_cls:
        result = run_claude("continue", session_id="sess_456")

    cmd = mock_cls.call_args[0][0]
    cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
    assert "-r" in cmd_str
    assert "sess_456" in cmd_str
    assert result.text == "Resumed!"


def test_timeout():
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.communicate.side_effect = [
        subprocess.TimeoutExpired("claude", 300),
        ("", ""),  # second call after kill()
    ]
    mock_proc.kill.return_value = None
    with patch("subprocess.Popen", return_value=mock_proc):
        result = run_claude("slow prompt")

    assert result.is_error
    assert "timed out" in result.text


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


def test_truncation():
    long_text = "x" * 5000
    output = json.dumps({"result": long_text, "session_id": "s1"})
    mock_proc = _mock_popen(stdout=output)
    with patch("subprocess.Popen", return_value=mock_proc):
        result = run_claude("big")

    assert len(result.text) <= 4000
    assert "truncated" in result.text


def test_unset_claudecode_env():
    """Verify CLAUDECODE is removed from subprocess env."""
    import os

    original = os.environ.get("CLAUDECODE")
    os.environ["CLAUDECODE"] = "test_value"
    try:
        mock_proc = _mock_popen(stdout='{"result":"ok"}')
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
    mock_proc = _mock_popen(stdout="plain text response")
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
        original_communicate = mock_proc.communicate

        def communicate_with_cancel(*a, **kw):
            # Simulate: while waiting, someone calls cancel_process
            cancel_process("conv_cancel_test")
            return original_communicate(*a, **kw)

        mock_proc.communicate = communicate_with_cancel
        result = run_claude("test", conversation_key="conv_cancel_test")

    assert not result.is_error
    assert "stopped" in result.text
