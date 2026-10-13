"""PiHarness: command line and parsing of real `pi --mode json` runs.

The fixtures in tests/fixtures/harness/pi were recorded on 2026-10-13 with
pi 1.2.0: tool_turn/resume against Strata, gpu_busy/bash_progress against a
fake server returning Strata's exact 503 body and a scripted bash call.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from yuki_conductor import skills
from yuki_conductor.config import PI_DEFAULT_MODEL
from yuki_conductor.harness import HARNESSES
from yuki_conductor.harness.base import RunRequest
from yuki_conductor.harness.pi import (
    GPU_BUSY_REPLY,
    PROGRESS_INTERVAL_S,
    PiHarness,
    PiStreamParser,
)
from yuki_conductor.harness.process import run_agent

FIXTURES = Path(__file__).parent / "fixtures" / "harness" / "pi"


def _lines(name: str) -> list[str]:
    return (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)


def _parse(name: str, clock=None):
    parser = PiStreamParser(clock=clock) if clock else PiStreamParser()
    events = [e for line in _lines(name) for e in parser.feed(line)]
    return events, parser.finish("".join(_lines(name)), fallback_session_id="fallback")


def test_registered():
    assert isinstance(HARNESSES["pi"], PiHarness)


# --------------------------------------------------------------------------
# launch
# --------------------------------------------------------------------------


def _launch(**kw):
    req = RunRequest(prompt="hello", session_id=None, model=None, cwd="/work",
                     conversation_key=None)
    for k, v in kw.items():
        setattr(req, k, v)
    return PiHarness().launch(req)


def test_new_conversation_command_line():
    launch = _launch()
    argv = launch.argv
    assert argv[:3] == ["pi", "--mode", "json"]
    assert "--no-approve" in argv
    assert argv[argv.index("--model") + 1] == PI_DEFAULT_MODEL
    assert "--session-id" not in argv
    # The prompt comes after `--`, so one starting with "-" isn't read as a flag.
    assert argv[-2:] == ["--", "hello"]


def test_resume_and_model_override():
    argv = _launch(session_id="sess-1", model="strata/other").argv
    assert argv[argv.index("--session-id") + 1] == "sess-1"
    assert argv[argv.index("--model") + 1] == "strata/other"


def test_system_prompt_is_the_core_prompt_only(tmp_path):
    launch = _launch(conversation_key="conv-9")
    path = launch.argv[launch.argv.index("--append-system-prompt") + 1]
    try:
        text = Path(path).read_text(encoding="utf-8")
    finally:
        Path(path).unlink()
    assert text == skills.core_prompt(conversation_key="conv-9")
    assert launch.temp_files == [path]


# --------------------------------------------------------------------------
# parsing real runs
# --------------------------------------------------------------------------


def test_tool_turn_against_strata():
    events, result = _parse("tool_turn")
    kinds = [e.kind for e in events]
    assert kinds == ["init", "thinking", "tool_use", "tool_result", "thinking", "text"]
    assert events[2].label == "read(note.txt)"
    assert events[3].label == "hello from the fixture"
    assert result.session_id == "yuki-fixture-1"
    assert not result.is_error
    assert result.text == 'The file `note.txt` contains a single greeting line: "hello from the fixture".'
    # input counts fresh plus cache-read tokens, summed over both model calls
    assert (result.input_tokens, result.output_tokens) == (2443 + 17 + 2495, 50 + 33)
    assert result.cost_usd == 0.0


def test_resume_reuses_the_session():
    _, result = _parse("resume")
    assert result.session_id == "yuki-fixture-1"
    assert "note.txt" in result.text


def test_gpu_busy_is_an_error_with_a_readable_reply():
    """pi exits 0 here; the failure is only visible in stopReason."""
    events, result = _parse("gpu_busy")
    assert result.is_error
    assert result.text == GPU_BUSY_REPLY
    retries = [e for e in events if e.label.startswith("Retrying")]
    assert [e.detail["attempt"] for e in retries] == [1, 2, 3]
    assert "GPU is busy" in retries[0].label


def test_live_bash_output_becomes_throttled_progress():
    ticks = iter(range(0, 1000, int(PROGRESS_INTERVAL_S)))
    events, result = _parse("bash_progress", clock=lambda: next(ticks))
    partial = [e for e in events if e.detail.get("partial")]
    # One step per new last line, not one per (cumulative) update.
    assert [e.label for e in partial] == ["render 1/3", "render 2/3", "render 3/3"]
    assert result.text == "done"


def test_progress_is_throttled_within_the_interval():
    events, _ = _parse("bash_progress", clock=lambda: 0.0)
    assert [e.label for e in events if e.detail.get("partial")] == ["render 1/3"]


def _tool_run(output: str) -> list:
    parser = PiStreamParser(clock=lambda: 0.0)
    update = {"type": "tool_execution_update", "toolCallId": "c1",
              "partialResult": {"content": [{"type": "text", "text": output}]}}
    end = {"type": "tool_execution_end", "toolCallId": "c1", "isError": False,
           "result": {"content": [{"type": "text", "text": output}]}}
    return parser.feed(json.dumps(update)) + parser.feed(json.dumps(end))


def test_one_line_output_is_not_shown_twice():
    """Seen live in a real run: `echo x` produced the same line as progress and result."""
    events = _tool_run("yuki-e2e-ok\n")
    assert [e.label for e in events] == ["yuki-e2e-ok"]


def test_multi_line_output_still_gets_its_result_step():
    events = _tool_run("line 1\nline 2\n")
    assert [e.detail.get("partial", False) for e in events] == [True, False]


def test_other_errors_pass_through():
    parser = PiStreamParser()
    parser.feed(json.dumps({"type": "message_end", "message": {
        "role": "assistant", "content": [], "stopReason": "error",
        "errorMessage": "401: bad key"}}))
    result = parser.finish("", None)
    assert result.is_error and result.text == "401: bad key"


def test_length_stop_keeps_the_text_and_says_so():
    parser = PiStreamParser()
    parser.feed(json.dumps({"type": "message_end", "message": {
        "role": "assistant", "stopReason": "length",
        "content": [{"type": "text", "text": "partial answer"}]}}))
    result = parser.finish("", None)
    assert not result.is_error
    assert result.text.startswith("partial answer")
    assert "output limit" in result.text


def test_garbage_lines_are_ignored():
    parser = PiStreamParser()
    assert parser.feed("not json\n") == []
    assert parser.feed("[1, 2]\n") == []
    result = parser.finish("not json", "s")
    assert result.is_error and result.session_id == "s"


# --------------------------------------------------------------------------
# end to end through run_agent
# --------------------------------------------------------------------------


def _mock_popen(stdout: str, returncode: int = 0):
    proc = MagicMock(spec=subprocess.Popen)
    proc.stdout = iter(stdout.splitlines(keepends=True))
    proc.stderr = iter([])
    proc.wait.return_value = returncode
    proc.returncode = returncode
    proc.pid = 99999
    return proc


def test_run_agent_reports_gpu_busy_despite_exit_code_zero():
    proc = _mock_popen("".join(_lines("gpu_busy")))
    with patch("subprocess.Popen", return_value=proc) as popen:
        result = run_agent(HARNESSES["pi"], "hi", cwd="/work")
    assert result.is_error
    assert result.text == GPU_BUSY_REPLY
    prompt_file = popen.call_args.args[0][popen.call_args.args[0].index("--append-system-prompt") + 1]
    assert not Path(prompt_file).exists()


def test_run_agent_streams_events_and_returns_the_session():
    seen = []
    proc = _mock_popen("".join(_lines("tool_turn")))
    with patch("subprocess.Popen", return_value=proc):
        result = run_agent(HARNESSES["pi"], "hi", cwd="/work", on_event=seen.append)
    assert result.session_id == "yuki-fixture-1"
    assert [e.kind for e in seen][:3] == ["init", "thinking", "tool_use"]
