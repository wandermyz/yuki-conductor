"""Prompt rendering for task turns."""

from yuki_conductor import taskboard
from yuki_conductor.taskboard import prompts


def _task(**kw):
    kw.setdefault("use_worktree", False)
    return taskboard.store.create(title="Add a cache", project="proj", harness="claude-code", body="Use **redis**.", **kw)


def test_first_turn_has_the_task_and_comments():
    task = _task(attachments=["/up/spec.pdf"])
    user = taskboard.store.add_comment(task.id, "user", "also metrics")
    taskboard.store.add_comment(task.id, "system", "noise")
    text = prompts.first_turn(task, taskboard.store.comments(task.id), "/repo")
    assert f"# Task #{task.id}: Add a cache" in text
    assert "working directory: /repo" in text
    assert "<attachment>/up/spec.pdf</attachment>" in text
    assert "Use **redis**." in text
    assert f"comment #{user.id} · user" in text
    assert "noise" not in text


def test_first_turn_names_the_worktree_branch():
    task = _task()
    task = taskboard.store.update_fields(task.id, branch="task/1-add-a-cache")
    assert "branch `task/1-add-a-cache`" in prompts.first_turn(task, [], "/wt")


def test_follow_up_renders_quotes_github_and_attachments():
    task = _task()
    first = taskboard.store.add_comment(task.id, "agent", "which TTL?")
    reply = taskboard.store.add_comment(
        task.id, "user", "> which TTL?\n\n5 minutes", quote_comment_id=first.id, attachments=["/up/a.png"]
    )
    gh = taskboard.store.add_comment(
        task.id, "github", "src/x.py:3 nit", external_url="https://github.com/o/r/pull/1#c", external_author="octo"
    )
    text = prompts.follow_up([reply, gh])
    assert text.startswith("New comments on your task")
    assert f"(replying to comment #{first.id})" in text
    assert "<attachment>/up/a.png</attachment>" in text
    assert "github · octo" in text
    assert "https://github.com/o/r/pull/1#c" in text


def test_full_context_includes_system_history():
    task = _task()
    taskboard.store.add_comment(task.id, "system", "To Do → In Progress: picked up")
    taskboard.store.add_comment(task.id, "agent", "did half")
    text = prompts.full_context(task, taskboard.store.comments(task.id), "/repo")
    assert "session is gone" in text
    assert "Use **redis**." in text
    assert "picked up" in text and "did half" in text


def test_pasted_file_markers_point_at_the_file_on_disk(monkeypatch, tmp_path):
    shot = tmp_path / "pasted.png"
    monkeypatch.setattr(prompts, "resolve_file", lambda fid: shot if fid == "abc123" else None)
    task = taskboard.store.create(
        title="t", project="proj", harness="claude-code", use_worktree=False,
        body="See ![pasted.png](/api/files/abc123) and ![gone](/api/files/fff)",
    )
    comment = taskboard.store.add_comment(task.id, "user", "again ![x](/api/files/abc123)")
    text = prompts.first_turn(task, [comment], "/repo")
    assert f"![pasted.png]({shot})" in text
    assert f"![x]({shot})" in text
    assert "![gone](/api/files/fff)" in text


def test_reopened_follow_up_names_the_new_worktree():
    task = _task()
    taskboard.store.add_comment(task.id, "agent", "done")
    assert not prompts.was_reopened(taskboard.store.comments(task.id))
    taskboard.store.add_comment(task.id, "system", "Done → To Do: reopened")
    note = taskboard.store.add_comment(task.id, "user", "one more thing")
    comments = taskboard.store.comments(task.id)
    assert prompts.was_reopened(comments)
    text = prompts.follow_up([note], reopened=True, cwd="/wt2")
    assert "reopened" in text and "/wt2" in text and "one more thing" in text
    taskboard.store.add_comment(task.id, "agent", "on it")
    assert not prompts.was_reopened(taskboard.store.comments(task.id))
