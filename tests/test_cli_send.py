"""Tests for the `yuki-conductor send` CLI subcommand."""

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from yuki_conductor.cli import main


def _mock_response(payload=None):
    resp = MagicMock()
    resp.read.return_value = json.dumps(
        payload or {"ok": True, "conversation_id": "conv-1", "created": False}
    ).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _captured_request(mock_urlopen):
    return mock_urlopen.call_args[0][0]


def test_send_posts_to_named_conversation(capsys):
    with patch("urllib.request.urlopen", return_value=_mock_response()) as m:
        main(["send", "-c", "conv-1", "hello there"])

    body = json.loads(_captured_request(m).data)
    assert body == {"text": "hello there", "conversation_id": "conv-1", "title": None}
    assert "conv-1" in capsys.readouterr().out


def test_send_without_conversation_omits_id():
    with patch("urllib.request.urlopen", return_value=_mock_response()) as m:
        main(["send", "--title", "Alert", "something happened"])

    body = json.loads(_captured_request(m).data)
    assert body["conversation_id"] is None
    assert body["title"] == "Alert"


def test_send_reads_stdin_for_dash(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("line one\nline two\n"))
    with patch("urllib.request.urlopen", return_value=_mock_response()) as m:
        main(["send", "-"])

    assert json.loads(_captured_request(m).data)["text"] == "line one\nline two\n"


def test_send_rejects_empty_text(capsys):
    with patch("urllib.request.urlopen") as m, pytest.raises(SystemExit) as exc:
        main(["send", "   "])
    assert exc.value.code == 1
    m.assert_not_called()
    assert "empty message" in capsys.readouterr().err.lower()


def test_send_reports_unreachable_daemon(capsys):
    with (
        patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")),
        pytest.raises(SystemExit) as exc,
    ):
        main(["send", "hi"])
    assert exc.value.code == 1
    assert "not reachable" in capsys.readouterr().err


def test_send_reports_http_error(capsys):
    err = urllib.error.HTTPError(
        url="http://localhost/api/push",
        code=404,
        msg="Not Found",
        hdrs=None,
        fp=io.BytesIO(b'{"detail":"Conversation not found"}'),
    )
    with patch("urllib.request.urlopen", side_effect=err), pytest.raises(SystemExit):
        main(["send", "-c", "nope", "hi"])
    assert "404" in capsys.readouterr().err
