"""Gzip for the web server's text responses, and only those.

The UI is often used over a slow link (a phone on Tailscale), where the JSON
lists and the frontend bundle dominate load time and shrink 5-10x. Starlette's
`GZipMiddleware` compresses every response, though: images and media gain
nothing, and a 206 range response would be compressed under a `Content-Range`
that describes the uncompressed bytes. So this compresses by content type and
leaves partial responses alone.
"""

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware, GZipResponder
from starlette.types import Message, Receive, Scope, Send

_TEXT_TYPES = (
    "application/json",
    "application/javascript",
    "application/manifest+json",
    "image/svg+xml",
    "text/",
)


def compressible(start: Message) -> bool:
    """Whether a response, by its `http.response.start` message, is worth gzipping."""
    if start.get("status") == 206:
        return False
    ctype = Headers(raw=start.get("headers", [])).get("content-type", "")
    return ctype.startswith(_TEXT_TYPES) and not ctype.startswith("text/event-stream")


class _TextResponder(GZipResponder):
    async def send_with_compression(self, message: Message) -> None:
        await super().send_with_compression(message)
        # The start message is held back until the first body chunk, which is
        # where the base class reads `content_type_is_excluded`.
        if message["type"] == "http.response.start" and not compressible(message):
            self.content_type_is_excluded = True


class TextGZipMiddleware(GZipMiddleware):
    def __init__(self, app, minimum_size: int = 1024, compresslevel: int = 6) -> None:
        super().__init__(app, minimum_size=minimum_size, compresslevel=compresslevel)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and "gzip" in Headers(scope=scope).get("accept-encoding", ""):
            responder = _TextResponder(
                self.app, self.minimum_size, compresslevel=self.compresslevel
            )
            await responder(scope, receive, send)
        else:
            await self.app(scope, receive, send)
