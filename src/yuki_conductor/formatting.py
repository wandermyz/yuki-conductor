"""Convert Markdown to Slack mrkdwn format."""

import re

# Sentinel unlikely to appear in real text
_BOLD = "\x00BOLD\x00"


def markdown_to_mrkdwn(md: str) -> str:
    """Convert common Markdown syntax to Slack's mrkdwn format.

    Handles: headings, bold, italic, links, and bullet lists.
    """
    text = md
    # Bold **text** → placeholder (must come before headings/italic)
    text = re.sub(r"\*\*(.+?)\*\*", rf"{_BOLD}\1{_BOLD}", text)
    # Headers → bold placeholder
    text = re.sub(r"^#{1,6}\s+(.+)$", rf"{_BOLD}\1{_BOLD}", text, flags=re.MULTILINE)
    # Italic *text* (single, not double) → _text_
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"_\1_", text)
    # Restore bold placeholders → *text*
    text = text.replace(_BOLD, "*")
    # Links [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)
    # Bullet lists: - or * at start of line → •
    text = re.sub(r"^(\s*)[-*]\s+", r"\1• ", text, flags=re.MULTILINE)
    return text
