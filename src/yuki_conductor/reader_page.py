"""Server-rendered article page for one assistant reply (`/read/<conv>/<msg>`).

Safari decides whether to offer Reader view from the HTML it first receives, so
this page is plain semantic markup rendered here, with no JavaScript: a single
<article> holding an <h1>, a byline, and the reply as HTML.
"""

from __future__ import annotations

import html
from datetime import datetime

import markdown

from yuki_conductor.conversation_store import StoredMessage

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")

_CSS = """
:root { color-scheme: light dark; --fg: #1d1d1f; --muted: #6e6e73; --bg: #fff;
  --code: rgba(127,127,127,.12); --border: #d2d2d7; --accent: #007aff; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #f5f5f7; --muted: #a1a1a6; --bg: #16171d; --border: #3a3a3c; --accent: #0a84ff; }
}
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 18px/1.65 -apple-system, system-ui, sans-serif; -webkit-text-size-adjust: 100%; }
main { max-width: 42rem; margin: 0 auto; padding: 2rem 1.25rem 4rem; }
h1 { font-size: 1.8rem; line-height: 1.25; margin: 0 0 .25rem; }
.byline { color: var(--muted); font-size: .9rem; margin: 0 0 2rem; }
a { color: var(--accent); }
pre { overflow-x: auto; padding: .75rem 1rem; border-radius: 8px; background: var(--code); font-size: .85em; }
code { font-family: ui-monospace, monospace; }
:not(pre) > code { background: var(--code); padding: .1em .3em; border-radius: 4px; font-size: .9em; }
table { border-collapse: collapse; display: block; overflow-x: auto; }
th, td { border: 1px solid var(--border); padding: .3rem .6rem; }
img { max-width: 100%; }
blockquote { margin-left: 0; padding-left: 1rem; border-left: 3px solid var(--border); color: var(--muted); }
"""


def _render_markdown(text: str) -> str:
    md = markdown.Markdown(extensions=["extra", "sane_lists"])
    # Show raw HTML in a reply as text rather than injecting it into the page.
    md.preprocessors.deregister("html_block")
    md.inlinePatterns.deregister("html")
    return md.convert(text)


def _render_attachment(a) -> str:
    url = html.escape(a.url)
    name = html.escape(a.filename)
    is_image = (a.mime_type or "").startswith("image/") or a.filename.lower().endswith(_IMAGE_EXTS)
    if is_image:
        return f'<figure><img src="{url}" alt="{name}"></figure>'
    return f'<p><a href="{url}" download>{name}</a></p>'


def render_reader_page(msg: StoredMessage, title: str | None) -> str:
    title = html.escape(title or "Reply")
    when = datetime.fromtimestamp(msg.created_at).astimezone()
    body = _render_markdown(msg.text) + "".join(_render_attachment(a) for a in msg.attachments)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta property="og:type" content="article">
<meta property="og:title" content="{title}">
<style>{_CSS}</style>
</head>
<body>
<main>
<article>
<header>
<h1>{title}</h1>
<p class="byline"><time datetime="{when.isoformat()}">{when:%b} {when.day}, {when.year}, {when.hour % 12 or 12}:{when:%M %p}</time></p>
</header>
{body}
</article>
</main>
</body>
</html>
"""
