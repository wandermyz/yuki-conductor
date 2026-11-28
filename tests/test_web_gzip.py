"""Gzip applies to text responses only (web_gzip)."""

import gzip

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from yuki_conductor.web_gzip import TextGZipMiddleware

BIG = b"x" * 5000


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(TextGZipMiddleware)

    @app.get("/json")
    def json_():
        return {"data": "x" * 5000}

    @app.get("/small")
    def small():
        return {"ok": True}

    @app.get("/png")
    def png():
        return Response(BIG, media_type="image/png")

    @app.get("/js")
    def js():
        return Response(BIG, media_type="text/javascript")

    @app.get("/range")
    def range_():
        return Response(
            BIG,
            status_code=206,
            media_type="text/plain",
            headers={"Content-Range": f"bytes 0-4999/{len(BIG) * 2}"},
        )

    return TestClient(app)


def _get(client, path, encoding="gzip"):
    # The test client decodes gzip itself; read the raw bytes to see the wire.
    with client.stream("GET", path, headers={"Accept-Encoding": encoding}) as r:
        return r, b"".join(r.iter_raw())


def test_text_responses_are_gzipped():
    client = _client()
    for path in ("/json", "/js"):
        r, raw = _get(client, path)
        assert r.headers["content-encoding"] == "gzip"
        assert len(raw) < 1000
        assert len(gzip.decompress(raw)) >= 5000


def test_other_responses_pass_through():
    client = _client()
    for path in ("/small", "/png", "/range"):
        r, _ = _get(client, path)
        assert "content-encoding" not in r.headers, path
    r, raw = _get(client, "/json", encoding="identity")
    assert "content-encoding" not in r.headers and len(raw) > 5000
