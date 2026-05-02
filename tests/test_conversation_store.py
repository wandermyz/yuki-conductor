"""Tests for the web ConversationStore."""

from pathlib import Path

from yuki_conductor.conversation_store import ConversationStore, StoredAttachment


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
