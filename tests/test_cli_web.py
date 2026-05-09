"""Tests for CLI web commands."""

import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from yuki_conductor.cli import main


def _mock_response(status=200):
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = b'{"ok": true}'
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_web_rebuild_via_api(capsys):
    with patch("urllib.request.urlopen", return_value=_mock_response()):
        main(["web", "rebuild"])

    captured = capsys.readouterr()
    assert "reload automatically" in captured.out


def test_web_rebuild_fallback_when_daemon_down(capsys):
    with (
        patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")),
        patch("yuki_conductor.config.build_web_frontend", return_value=True),
    ):
        main(["web", "rebuild"])

    captured = capsys.readouterr()
    assert "Daemon not reachable" in captured.out
    assert "rebuilt" in captured.out


def test_web_rebuild_fallback_failure(capsys):
    with (
        patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")),
        patch("yuki_conductor.config.build_web_frontend", return_value=False),
    ):
        with pytest.raises(SystemExit, match="1"):
            main(["web", "rebuild"])


def test_web_no_subcommand():
    with pytest.raises(SystemExit):
        main(["web"])
