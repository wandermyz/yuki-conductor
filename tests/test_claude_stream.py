"""Tests for stream-json line normalization."""

import json

from yuki_conductor.harness.claude_code import build_result, parse_stream_line


def _line(record: dict) -> str:
    return json.dumps(record) + "\n"


def test_init_line_yields_init_event():
    events, final, _ = parse_stream_line(
        _line({"type": "system", "subtype": "init", "model": "opus", "session_id": "s1"})
    )
    assert final is None
    assert len(events) == 1
    assert events[0].kind == "init"
    assert "opus" in events[0].label
    assert events[0].detail["session_id"] == "s1"


def test_result_line_is_returned_as_final():
    events, final, _ = parse_stream_line(
        _line({"type": "result", "result": "all done", "session_id": "s1"})
    )
    assert events == []
    assert final is not None
    assert final["result"] == "all done"


def test_tool_use_label_prefers_identifying_argument():
    events, _, _ = parse_stream_line(
        _line({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "pytest -q", "description": "run tests"}},
        ]}})
    )
    assert events[0].kind == "tool_use"
    assert events[0].label == "Bash(pytest -q)"
    assert events[0].detail["name"] == "Bash"


def test_tool_use_without_recognized_argument_falls_back_to_name():
    events, _, _ = parse_stream_line(
        _line({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Mystery", "input": {"count": 3}},
        ]}})
    )
    assert events[0].label == "Mystery"


def test_thinking_and_text_blocks_are_separate_events():
    events, _, _ = parse_stream_line(
        _line({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "Here goes."},
        ]}})
    )
    assert [e.kind for e in events] == ["thinking", "text"]


def test_tool_result_blocks_flatten_list_content():
    events, _, _ = parse_stream_line(
        _line({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "42 matches"}]},
        ]}})
    )
    assert events[0].kind == "tool_result"
    assert events[0].detail["summary"] == "42 matches"
    assert events[0].detail["is_error"] is False


def test_error_tool_result_is_labelled():
    events, _, _ = parse_stream_line(
        _line({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "is_error": True, "content": "no such file"},
        ]}})
    )
    assert events[0].label.startswith("error: ")
    assert events[0].detail["is_error"] is True


def test_long_labels_are_truncated_to_one_line():
    events, _, _ = parse_stream_line(
        _line({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "line one\nline two " + "x" * 500},
        ]}})
    )
    assert "\n" not in events[0].label
    assert len(events[0].label) <= 120


def test_detail_keeps_full_text_and_tool_input():
    text = "line one\nline two " + "x" * 500
    command = "echo " + "y" * 500
    events, _, _ = parse_stream_line(
        _line({"type": "assistant", "message": {"content": [
            {"type": "text", "text": text},
            {"type": "thinking", "thinking": text},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": command}},
        ]}})
    )
    assert events[0].detail["text"] == text
    assert events[1].detail["text"] == text
    assert events[2].detail["summary"] == command
    assert len(events[2].label) <= len("Bash()") + 120


def test_tool_result_stays_truncated():
    events, _, _ = parse_stream_line(
        _line({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "z" * 500},
        ]}})
    )
    assert len(events[0].detail["summary"]) <= 120


def test_garbage_lines_are_ignored():
    """Stray non-JSON output must not abort a run."""
    assert parse_stream_line("not json at all") == ([], None, None)
    assert parse_stream_line("") == ([], None, None)
    assert parse_stream_line("[1, 2, 3]") == ([], None, None)


def test_unknown_message_types_produce_nothing():
    assert parse_stream_line(_line({"type": "something_new"})) == ([], None, None)


def test_events_are_json_serializable():
    events, _, _ = parse_stream_line(
        _line({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}},
        ]}})
    )
    payload = json.dumps(events[0].to_dict())
    assert json.loads(payload)["kind"] == "tool_use"


# --------------------------------------------------------------------------
# context window occupation
# --------------------------------------------------------------------------


def _assistant(usage=None, model="claude-opus-5", parent=None) -> str:
    return _line({
        "type": "assistant",
        "parent_tool_use_id": parent,
        "message": {
            "model": model,
            "usage": usage,
            "content": [{"type": "text", "text": "hi"}],
        },
    })


def test_assistant_line_is_returned_as_the_last_message():
    _, _, assistant = parse_stream_line(_assistant({"input_tokens": 3}))
    assert assistant["model"] == "claude-opus-5"


def test_subagent_assistant_is_not_the_conversation_context():
    """A subagent's own small context must not be read as the conversation's."""
    _, _, assistant = parse_stream_line(_assistant({"input_tokens": 3}, parent="t1"))
    assert assistant is None


def test_context_is_the_last_call_not_the_turn_aggregate():
    """`usage` on the result record sums every call; the window is one call's size."""
    final = {
        "type": "result",
        "result": "done",
        "session_id": "s1",
        "num_turns": 3,
        "usage": {"input_tokens": 900, "cache_read_input_tokens": 40000, "output_tokens": 60},
        "modelUsage": {"claude-opus-5": {"contextWindow": 200000}},
    }
    last = {"model": "claude-opus-5", "usage": {
        "input_tokens": 5,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 39000,
        "output_tokens": 20,
    }}
    result = build_result(final, "", None, last)
    assert result.context_tokens == 40005
    assert result.context_window == 200000


def test_single_turn_run_falls_back_to_the_result_usage():
    result = build_result(
        {
            "type": "result",
            "result": "done",
            "num_turns": 1,
            "usage": {"input_tokens": 2, "cache_read_input_tokens": 31831, "output_tokens": 4},
        },
        "",
        None,
    )
    assert result.context_tokens == 31833
    assert result.context_window is None


def test_multi_turn_run_without_assistant_usage_reports_no_context():
    """Guessing from the aggregate would overstate how full the window is."""
    result = build_result(
        {
            "type": "result",
            "result": "done",
            "num_turns": 4,
            "usage": {"input_tokens": 900, "cache_read_input_tokens": 40000},
        },
        "",
        None,
    )
    assert result.context_tokens is None


def test_window_matches_the_model_that_answered():
    final = {
        "type": "result",
        "result": "done",
        "num_turns": 1,
        "usage": {"input_tokens": 1},
        "modelUsage": {
            "claude-haiku-5-5": {"contextWindow": 1000000},
            "claude-opus-5": {"contextWindow": 200000},
        },
    }
    last = {"model": "claude-opus-5", "usage": {"input_tokens": 1}}
    assert build_result(final, "", None, last).context_window == 200000
    # An unknown model name still gets the widest window the run reported.
    other = {"model": "something-new", "usage": {"input_tokens": 1}}
    assert build_result(final, "", None, other).context_window == 1000000
