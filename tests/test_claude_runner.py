"""Tests for claude_runner module."""

import json
import subprocess
from unittest.mock import patch

from yuki_conductor.claude_runner import run_claude


def _mock_run(stdout="", stderr="", returncode=0, **kwargs):
    """Create a mock CompletedProcess."""
    return subprocess.CompletedProcess(
        args=["claude"], stdout=stdout, stderr=stderr, returncode=returncode
    )


def test_basic_call():
    output = json.dumps({"result": "Hello!", "session_id": "sess_123"})
    with patch("subprocess.run", return_value=_mock_run(stdout=output)) as mock:
        result = run_claude("hi")

    assert result.text == "Hello!"
    assert result.session_id == "sess_123"
    assert not result.is_error

    cmd = mock.call_args[0][0]
    assert "claude" in cmd
    assert "-p" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--output-format" in cmd
    assert "json" in cmd
    assert "hi" in cmd


def test_resume_session():
    output = json.dumps({"result": "Resumed!", "session_id": "sess_456"})
    with patch("subprocess.run", return_value=_mock_run(stdout=output)) as mock:
        result = run_claude("continue", session_id="sess_456")

    cmd = mock.call_args[0][0]
    assert "-r" in cmd
    assert "sess_456" in cmd
    assert result.text == "Resumed!"


def test_timeout():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 300)):
        result = run_claude("slow prompt")

    assert result.is_error
    assert "timed out" in result.text


def test_cli_not_found():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = run_claude("hello")

    assert result.is_error
    assert "not found" in result.text


def test_nonzero_exit():
    with patch("subprocess.run", return_value=_mock_run(returncode=1, stderr="Some error")):
        result = run_claude("bad prompt")

    assert result.is_error
    assert "Some error" in result.text


def test_truncation():
    long_text = "x" * 5000
    output = json.dumps({"result": long_text, "session_id": "s1"})
    with patch("subprocess.run", return_value=_mock_run(stdout=output)):
        result = run_claude("big")

    assert len(result.text) <= 4000
    assert "truncated" in result.text


def test_unset_claudecode_env():
    """Verify CLAUDECODE is removed from subprocess env."""
    import os

    original = os.environ.get("CLAUDECODE")
    os.environ["CLAUDECODE"] = "test_value"
    try:
        with patch("subprocess.run", return_value=_mock_run(stdout='{"result":"ok"}')) as mock:
            run_claude("test")

        env = mock.call_args[1]["env"]
        assert "CLAUDECODE" not in env
    finally:
        if original is None:
            os.environ.pop("CLAUDECODE", None)
        else:
            os.environ["CLAUDECODE"] = original


def test_non_json_output():
    with patch("subprocess.run", return_value=_mock_run(stdout="plain text response")):
        result = run_claude("test")

    assert result.text == "plain text response"
    assert not result.is_error
