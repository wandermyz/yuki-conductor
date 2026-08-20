"""Tests for cron.yaml parsing, schedule description, and surgical edits."""

from yuki_conductor.cron_config import (
    CronTask,
    describe_schedule,
    load_tasks,
    set_task_field,
    truncate_prompt,
)

SAMPLE = """\
# a comment that must survive
tasks:
  - name: morning-briefing
    schedule: "0 9 * * 1-5"
    description: "Morning briefing"
    prompt: >
      Give me a briefing.
      Keep it short.
    origin_conversation: conv-abc

  - name: weekly-check
    display_name: "Weekly check"
    schedule: "30 10 * * 1"
    prompt: "Check deps."
"""


def _write(tmp_path, text=SAMPLE):
    path = tmp_path / "cron.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_tasks_parses_all_fields(tmp_path):
    tasks = load_tasks(_write(tmp_path))
    assert [t.name for t in tasks] == ["morning-briefing", "weekly-check"]
    assert tasks[0].origin_conversation == "conv-abc"
    assert tasks[0].display_name is None
    assert tasks[1].display_name == "Weekly check"


def test_load_tasks_skips_invalid_entries(tmp_path):
    path = _write(
        tmp_path,
        'tasks:\n'
        '  - name: ok\n    schedule: "0 9 * * *"\n    prompt: "hi"\n'
        '  - name: bad-cron\n    schedule: "not a cron"\n    prompt: "hi"\n'
        '  - name: no-prompt\n    schedule: "0 9 * * *"\n',
    )
    assert [t.name for t in load_tasks(path)] == ["ok"]


def test_load_tasks_missing_file(tmp_path):
    assert load_tasks(tmp_path / "nope.yaml") == []


def test_label_falls_back_through_display_name_description_prompt():
    base = dict(name="t", schedule="* * * * *", prompt="do a thing")
    assert CronTask(**base, description="", display_name="Named").label == "Named"
    assert CronTask(**base, description="Described").label == "Described"
    assert CronTask(**base, description="").label == "do a thing"


def test_truncate_prompt_collapses_and_elides():
    assert truncate_prompt("hello\n  world") == "hello world"
    long = "word " * 40
    out = truncate_prompt(long, limit=20)
    assert len(out) <= 20 and out.endswith("…")


def test_describe_schedule_common_cases():
    assert describe_schedule("0 9 * * 1-5") == (
        "at 09:00 on Monday through Friday"
    )
    assert describe_schedule("30 10 * * 1") == "at 10:30 on Monday"
    assert describe_schedule("0 0 * * *") == "at 00:00 every day"
    assert describe_schedule("*/15 * * * *") == "every 15 minutes every day"
    assert describe_schedule("5 * * * *") == "at :05 past every hour every day"
    assert describe_schedule("0 9 1 * *") == "at 09:00 on day-of-month 1"


def test_describe_schedule_hour_ranges_and_lists():
    assert describe_schedule("17 9-23 * * 1-5") == (
        "hourly from 09:17 to 23:17 on Monday through Friday"
    )
    assert describe_schedule("0 9,17 * * *") == "at 09:00 and 17:00 every day"
    assert describe_schedule("30 */4 * * *") == "at :30 past every 4 hours every day"
    assert describe_schedule("0 12 * * 0,6") == "at 12:00 on Sunday and Saturday"


def test_describe_schedule_passthrough_on_garbage():
    assert describe_schedule("nonsense") == "nonsense"


def test_set_task_field_adds_and_updates_preserving_file(tmp_path):
    path = _write(tmp_path)

    assert set_task_field("morning-briefing", "display_name", "Briefing", path)
    text = path.read_text(encoding="utf-8")
    assert "# a comment that must survive" in text
    # The folded prompt block is untouched.
    assert "      Give me a briefing.\n      Keep it short.\n" in text

    tasks = load_tasks(path)
    assert tasks[0].display_name == "Briefing"
    # The other task is unaffected.
    assert tasks[1].display_name == "Weekly check"

    # Updating replaces in place rather than duplicating the key.
    assert set_task_field("morning-briefing", "display_name", "Renamed", path)
    assert path.read_text(encoding="utf-8").count("display_name:") == 2
    assert load_tasks(path)[0].display_name == "Renamed"


def test_set_task_field_clears_with_none(tmp_path):
    path = _write(tmp_path)
    assert set_task_field("weekly-check", "display_name", None, path)
    tasks = load_tasks(path)
    assert tasks[1].display_name is None
    assert tasks[1].label == "Check deps."


def test_set_task_field_quotes_awkward_values(tmp_path):
    path = _write(tmp_path)
    assert set_task_field("weekly-check", "display_name", 'He said: "hi" #1', path)
    assert load_tasks(path)[1].display_name == 'He said: "hi" #1'


def test_set_task_field_unknown_task(tmp_path):
    assert not set_task_field("nope", "display_name", "x", _write(tmp_path))


def test_set_task_field_rejects_non_editable_field(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        set_task_field("weekly-check", "prompt", "evil", _write(tmp_path))
