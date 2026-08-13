"""Tests for stream-json line normalization."""

import json

from yuki_conductor.stream_events import parse_stream_line


def _line(record: dict) -> str:
    return json.dumps(record) + "\n"


def test_init_line_yields_init_event():
    events, final = parse_stream_line(
        _line({"type": "system", "subtype": "init", "model": "opus", "session_id": "s1"})
    )
    assert final is None
    assert len(events) == 1
    assert events[0].kind == "init"
    assert "opus" in events[0].label
    assert events[0].detail["session_id"] == "s1"


def test_result_line_is_returned_as_final():
    events, final = parse_stream_line(
        _line({"type": "result", "result": "all done", "session_id": "s1"})
    )
    assert events == []
    assert final is not None
    assert final["result"] == "all done"


def test_tool_use_label_prefers_identifying_argument():
    events, _ = parse_stream_line(
        _line({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "pytest -q", "description": "run tests"}},
        ]}})
    )
    assert events[0].kind == "tool_use"
    assert events[0].label == "Bash(pytest -q)"
    assert events[0].detail["name"] == "Bash"


def test_tool_use_without_recognized_argument_falls_back_to_name():
    events, _ = parse_stream_line(
        _line({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Mystery", "input": {"count": 3}},
        ]}})
    )
    assert events[0].label == "Mystery"


def test_thinking_and_text_blocks_are_separate_events():
    events, _ = parse_stream_line(
        _line({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "Here goes."},
        ]}})
    )
    assert [e.kind for e in events] == ["thinking", "text"]


def test_tool_result_blocks_flatten_list_content():
    events, _ = parse_stream_line(
        _line({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"type": "text", "text": "42 matches"}]},
        ]}})
    )
    assert events[0].kind == "tool_result"
    assert events[0].detail["summary"] == "42 matches"
    assert events[0].detail["is_error"] is False


def test_error_tool_result_is_labelled():
    events, _ = parse_stream_line(
        _line({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "is_error": True, "content": "no such file"},
        ]}})
    )
    assert events[0].label.startswith("error: ")
    assert events[0].detail["is_error"] is True


def test_long_labels_are_truncated_to_one_line():
    events, _ = parse_stream_line(
        _line({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "line one\nline two " + "x" * 500},
        ]}})
    )
    assert "\n" not in events[0].label
    assert len(events[0].label) <= 120


def test_garbage_lines_are_ignored():
    """Stray non-JSON output must not abort a run."""
    assert parse_stream_line("not json at all") == ([], None)
    assert parse_stream_line("") == ([], None)
    assert parse_stream_line("[1, 2, 3]") == ([], None)


def test_unknown_message_types_produce_nothing():
    assert parse_stream_line(_line({"type": "something_new"})) == ([], None)


def test_events_are_json_serializable():
    events, _ = parse_stream_line(
        _line({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}},
        ]}})
    )
    payload = json.dumps(events[0].to_dict())
    assert json.loads(payload)["kind"] == "tool_use"
