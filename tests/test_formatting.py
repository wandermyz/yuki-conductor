"""Tests for Markdown → Slack mrkdwn conversion."""

from yuki_conductor.formatting import markdown_to_mrkdwn


def test_headings():
    assert markdown_to_mrkdwn("### Session Management") == "*Session Management*"
    assert markdown_to_mrkdwn("# Title") == "*Title*"


def test_bold():
    assert markdown_to_mrkdwn("**bold text**") == "*bold text*"


def test_bullet_list():
    assert markdown_to_mrkdwn("- item one") == "• item one"
    assert markdown_to_mrkdwn("* item two") == "• item two"


def test_nested_bullets_preserve_indent():
    assert markdown_to_mrkdwn("  - nested") == "  • nested"


def test_links():
    assert markdown_to_mrkdwn("[click](https://example.com)") == "<https://example.com|click>"


def test_combined():
    md = (
        "### Session Management\n"
        "- **Session listing** grouped by project in a collapsible sidebar"
    )
    expected = (
        "*Session Management*\n"
        "• *Session listing* grouped by project in a collapsible sidebar"
    )
    assert markdown_to_mrkdwn(md) == expected


def test_passthrough_code():
    assert markdown_to_mrkdwn("`code`") == "`code`"


def test_italic_single_star():
    assert markdown_to_mrkdwn("some *italic* text") == "some _italic_ text"
