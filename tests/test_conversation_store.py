"""Tests for the web ConversationStore."""

from pathlib import Path

from yuki_conductor.conversation_store import (
    SNIPPET_MARK,
    ConversationStore,
    StoredAttachment,
    fts_query,
)


def test_create_and_get(tmp_path: Path):
    s = ConversationStore(db_path=tmp_path / "t.db")
    c = s.create_conversation(title="hello")
    assert c.id
    fetched = s.get_conversation(c.id)
    assert fetched is not None
    assert fetched.title == "hello"
    assert fetched.platform == "web"


def test_list_filters_by_platform(tmp_path: Path):
    s = ConversationStore(db_path=tmp_path / "t.db")
    s.create_conversation(platform="web", title="a")
    s.create_conversation(platform="slack", title="b")
    web = s.list_conversations(platform="web")
    assert [c.title for c in web] == ["a"]


def test_messages_roundtrip(tmp_path: Path):
    s = ConversationStore(db_path=tmp_path / "t.db")
    c = s.create_conversation()
    s.add_message(c.id, role="user", text="hi")
    s.add_message(
        c.id,
        role="assistant",
        text="hello",
        attachments=[StoredAttachment(id="f1", filename="x.png", url="/api/files/f1")],
    )
    msgs = s.list_messages(c.id)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[1].attachments[0].filename == "x.png"


def test_delete_cascades(tmp_path: Path):
    s = ConversationStore(db_path=tmp_path / "t.db")
    c = s.create_conversation()
    s.add_message(c.id, role="user", text="hi")
    assert s.delete_conversation(c.id) is True
    assert s.get_conversation(c.id) is None
    assert s.list_messages(c.id) == []


def test_update_title_bumps_updated_at(tmp_path: Path):
    s = ConversationStore(db_path=tmp_path / "t.db")
    c = s.create_conversation(title="old")
    original_updated_at = c.updated_at
    assert s.update_conversation(c.id, title="new") is True
    refreshed = s.get_conversation(c.id)
    assert refreshed.title == "new"
    assert refreshed.updated_at >= original_updated_at


def test_harness_defaults_to_claude_code_and_round_trips(tmp_path: Path):
    s = ConversationStore(db_path=tmp_path / "t.db")
    default = s.create_conversation(platform="web")
    pi = s.create_conversation(platform="web", harness="pi")
    assert s.get_conversation(default.id).harness == "claude-code"
    assert s.get_conversation(pi.id).harness == "pi"
    assert {c.id: c.harness for c in s.list_conversations()} == {
        default.id: "claude-code", pi.id: "pi",
    }


def test_archive_hides_from_list_but_keeps_rows(tmp_path: Path):
    s = ConversationStore(db_path=tmp_path / "t.db")
    keep = s.create_conversation(title="keep")
    gone = s.create_conversation(title="gone")
    s.add_message(gone.id, role="user", text="still here")
    assert s.archive_conversation(gone.id)
    assert [c.id for c in s.list_conversations(platform="web")] == [keep.id]
    assert [c.id for c in s.list_conversations()] == [keep.id]
    archived = s.get_conversation(gone.id)
    assert archived is not None and archived.archived_at is not None
    assert [m.text for m in s.list_messages(gone.id)] == ["still here"]
    assert not s.archive_conversation(gone.id)  # already archived
    assert not s.archive_conversation("missing")


def test_context_usage_travels_with_the_messages(tmp_path: Path):
    """The chat reads context straight off the message list, so the join matters."""
    s = ConversationStore(db_path=tmp_path / "t.db")
    c = s.create_conversation()
    s.add_message(c.id, role="user", text="hi")
    reply = s.add_message(c.id, role="assistant", text="hello")
    s.record_usage(
        reply.id, c.id,
        input_tokens=120, output_tokens=20, cost_usd=0.01,
        context_tokens=40000, context_window=200000,
    )
    msgs = s.list_messages(c.id)
    assert [(m.context_tokens, m.context_window) for m in msgs] == [(None, None), (40000, 200000)]


def _fts_rowids(s: ConversationStore, match: str) -> list[int]:
    import sqlite3

    con = sqlite3.connect(str(s._db_path))
    try:
        return [
            r[0]
            for r in con.execute(
                "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", (match,)
            )
        ]
    finally:
        con.close()


def test_search_finds_message_and_marks_snippet(tmp_path: Path):
    s = ConversationStore(db_path=tmp_path / "t.db")
    c = s.create_conversation(title="deploy chat")
    s.add_message(c.id, role="user", text="why does the deploy fail?")
    m = s.add_message(c.id, role="assistant", text="The KeyError comes from config.py")
    s.add_message(s.create_conversation().id, role="user", text="unrelated")

    hits = s.search("keyerror")
    assert len(hits) == 1
    hit = hits[0]
    assert (hit.conversation_id, hit.title, hit.message_id) == (c.id, "deploy chat", m.id)
    start, end = SNIPPET_MARK
    assert f"{start}KeyError{end}" in hit.snippet
    assert hit.score > 0


def test_search_is_prefix_and_ands_terms(tmp_path: Path):
    s = ConversationStore(db_path=tmp_path / "t.db")
    c = s.create_conversation()
    s.add_message(c.id, role="user", text="tailscale serve returns 502")
    assert [h.conversation_id for h in s.search("tails 50")] == [c.id]
    assert s.search("tailscale nginx") == []


def test_search_groups_by_conversation_best_first(tmp_path: Path):
    s = ConversationStore(db_path=tmp_path / "t.db")
    weak = s.create_conversation(title="weak")
    s.add_message(weak.id, role="user", text="sqlite " + "filler words " * 50)
    strong = s.create_conversation(title="strong")
    s.add_message(strong.id, role="user", text="sqlite")
    best = s.add_message(strong.id, role="assistant", text="sqlite sqlite")

    hits = s.search("sqlite")
    assert [h.title for h in hits] == ["strong", "weak"]
    assert hits[0].message_id == best.id
    assert s.search("sqlite", limit=1)[0].title == "strong"


def test_search_chinese_with_spaces(tmp_path: Path):
    s = ConversationStore(db_path=tmp_path / "t.db")
    c = s.create_conversation()
    s.add_message(c.id, role="user", text="明天 会议纪要 发给我")
    assert [h.conversation_id for h in s.search("会议")] == [c.id]


def test_search_excludes_archived(tmp_path: Path):
    s = ConversationStore(db_path=tmp_path / "t.db")
    c = s.create_conversation()
    s.add_message(c.id, role="user", text="secret plan")
    assert len(s.search("secret")) == 1
    s.archive_conversation(c.id)
    assert s.search("secret") == []


def test_search_survives_fts_syntax(tmp_path: Path):
    s = ConversationStore(db_path=tmp_path / "t.db")
    c = s.create_conversation()
    s.add_message(c.id, role="user", text='he said "AND" (or NOT) near:x')
    for q in ['"', "AND", "(or", "NOT)", "near:x", "*", "a OR", "-x", "^", '""', "   ", ""]:
        s.search(q)  # must not raise
    assert len(s.search('"AND"')) == 1
    assert s.search("   ") == []
    assert s.search("* ^ -") == []


def test_fts_query_quotes_terms():
    assert fts_query('foo "bar') == '"foo"* """bar"*'
    assert fts_query("!!") is None


def test_fts_triggers_follow_insert_and_delete(tmp_path: Path):
    s = ConversationStore(db_path=tmp_path / "t.db")
    c = s.create_conversation()
    s.add_message(c.id, role="user", text="zeppelin")
    assert len(_fts_rowids(s, "zeppelin")) == 1
    s.delete_conversation(c.id)
    assert _fts_rowids(s, "zeppelin") == []
    assert s.search("zeppelin") == []


def test_fts_trigger_follows_update(tmp_path: Path):
    import sqlite3

    s = ConversationStore(db_path=tmp_path / "t.db")
    c = s.create_conversation()
    m = s.add_message(c.id, role="user", text="before")
    con = sqlite3.connect(str(s._db_path))
    con.execute("UPDATE messages SET text = 'after' WHERE id = ?", (m.id,))
    con.commit()
    con.close()
    assert s.search("before") == []
    assert [h.message_id for h in s.search("after")] == [m.id]


def test_fts_backfills_existing_messages(tmp_path: Path):
    import sqlite3

    db = tmp_path / "t.db"
    s = ConversationStore(db_path=db)
    c = s.create_conversation()
    s.add_message(c.id, role="user", text="history from before search existed")
    # Simulate a DB from before the index: drop it, then reopen the store.
    con = sqlite3.connect(str(db))
    for t in ("messages_fts_ai", "messages_fts_ad", "messages_fts_au"):
        con.execute(f"DROP TRIGGER {t}")
    con.execute("DROP TABLE messages_fts")
    con.commit()
    con.close()

    s2 = ConversationStore(db_path=db)
    assert [h.conversation_id for h in s2.search("history")] == [c.id]
    # Reopening again doesn't index anything twice.
    s3 = ConversationStore(db_path=db)
    assert len(_fts_rowids(s3, "history")) == 1
