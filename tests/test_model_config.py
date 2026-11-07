"""Alias identity, credential handling and per-run routing isolation."""

import json
import os
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from yuki_conductor.harness.base import RunRequest
from yuki_conductor.harness.claude_code import ClaudeCodeHarness
from yuki_conductor.harness.codex import CodexHarness
from yuki_conductor.harness.process import run_agent
from yuki_conductor.model_config import ModelConfigError, load_harness_models


@pytest.fixture
def endpoints(isolated_models, monkeypatch, tmp_path):
    monkeypatch.setenv("ROUTE_GH", "gh-test-credential")
    secret = tmp_path / "green-key"
    secret.write_text("green-test-credential\n", encoding="utf-8")
    data = yaml.safe_load(isolated_models.read_text())
    for harness, section in data["harness"].items():
        suffix = "/v1" if harness == "codex" else ""
        section["connections"].update({
            "gh": {"type": "endpoint", "base_url": f"http://localhost:4000{suffix}",
                   "api_key": {"env": "ROUTE_GH"}},
            "green": {"type": "endpoint", "base_url": f"http://localhost:4001{suffix}",
                      "api_key": {"file": "green-key"}},
        })
        model = "gpt-6.1-sol" if harness == "codex" else "claude-sonnet-5-5"
        for route in ("gh", "green"):
            section["aliases"][f"same-{route}"] = {
                "label": f"same ({route})", "model": model, "connection": route,
            }
        section["default"] = "same-gh"
    isolated_models.write_text(yaml.safe_dump(data), encoding="utf-8")
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return isolated_models


def _launch(harness, alias=None, session_id=None):
    return harness.launch(RunRequest("hello", session_id, alias, str(Path.cwd()), None))


def _cleanup(launch):
    for file in launch.temp_files:
        Path(file).unlink(missing_ok=True)


def _profile(launch):
    return tomllib.loads(Path(launch.temp_files[0]).read_text(encoding="utf-8"))


def _settings(launch):
    path = launch.argv[launch.argv.index("--settings") + 1]
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_same_model_keeps_distinct_alias_ids_and_labels(endpoints):
    h = CodexHarness()
    picks = {m.id: m.label for m in h.models()}
    assert picks["same-gh"] == "same (gh)"
    assert picks["same-green"] == "same (green)"
    assert h.default_model() == "same-gh"
    assert h.default_label() == "same (gh)"
    config = load_harness_models("codex")
    assert config.resolve("same-gh").model == config.resolve("same-green").model
    assert config.resolve("same-green").connection.api_key() == "green-test-credential"


@pytest.mark.parametrize("harness", [ClaudeCodeHarness(), CodexHarness()])
def test_concurrent_routes_are_isolated_and_never_serialize_keys(endpoints, harness):
    parent = dict(os.environ)
    with ThreadPoolExecutor(max_workers=2) as pool:
        launches = list(pool.map(lambda a: _launch(harness, a, "existing-session"),
                                 ["same-gh", "same-green"]))
    try:
        for launch, port, secret in zip(launches, (4000, 4001),
                                        ("gh-test-credential", "green-test-credential"), strict=True):
            assert launch.env["YUKI_MODEL_API_KEY"] == secret
            config = _settings(launch) if harness.name == "claude-code" else _profile(launch)
            if harness.name == "claude-code":
                assert config["env"]["ANTHROPIC_BASE_URL"] == f"http://localhost:{port}"
                assert config["env"]["ANTHROPIC_AUTH_TOKEN"] == ""
                assert "YUKI_MODEL_API_KEY" in config["apiKeyHelper"]
                assert "-r" in launch.argv
            else:
                assert config["model_providers"]["yuki"]["base_url"] == f"http://localhost:{port}/v1"
                assert config["model_providers"]["yuki"]["env_key"] == "YUKI_MODEL_API_KEY"
                assert config["model_providers"]["yuki"]["requires_openai_auth"] is False
                assert "forced_login_method" not in config
                assert "resume" in launch.argv
            assert secret not in repr(launch)
            assert secret not in " ".join(launch.argv)
            for path in launch.temp_files:
                assert secret not in Path(path).read_text(encoding="utf-8")
        assert dict(os.environ) == parent
    finally:
        for launch in launches:
            _cleanup(launch)


@pytest.mark.parametrize("harness,alias", [(ClaudeCodeHarness(), "sonnet"), (CodexHarness(), "sol")])
def test_subscription_overrides_inherited_routing(endpoints, monkeypatch, harness, alias):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                 "CLAUDE_CODE_USE_BEDROCK", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                 "YUKI_MODEL_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.setenv(name, "wrong-route")
    launch = _launch(harness, alias)
    try:
        assert "YUKI_MODEL_API_KEY" not in launch.env
        if harness.name == "claude-code":
            config = _settings(launch)
            assert config["apiKeyHelper"] == ""
            assert config["forceLoginMethod"] == "claudeai"
            assert config["env"]["ANTHROPIC_BASE_URL"] == ""
            assert "ANTHROPIC_AUTH_TOKEN" not in launch.env
            assert "CLAUDE_CODE_OAUTH_TOKEN" not in launch.env
            assert config["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == ""
        else:
            config = _profile(launch)
            assert config["model_provider"] == "openai"
            assert config["forced_login_method"] == "chatgpt"
            assert config["openai_base_url"] == "https://chatgpt.com/backend-api/codex"
            assert "OPENAI_BASE_URL" not in launch.env
    finally:
        _cleanup(launch)


@pytest.fixture
def subscription_token(endpoints):
    token_file = endpoints.parent / "subscription-token"
    token_file.write_text("subscription-test-token\n", encoding="utf-8")
    data = yaml.safe_load(endpoints.read_text())
    section = data["harness"]["claude-code"]
    connection = section["aliases"]["sonnet"]["connection"]
    section["connections"][connection]["oauth_token"] = {"file": token_file.name}
    endpoints.write_text(yaml.safe_dump(data))
    return token_file


def test_subscription_token_is_private_and_does_not_leak_to_endpoint(subscription_token, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "wrong-inherited-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "wrong-api-key")
    parent = dict(os.environ)
    launch = _launch(ClaudeCodeHarness(), "sonnet", "existing-session")
    try:
        secret = "subscription-test-token"
        assert launch.env["CLAUDE_CODE_OAUTH_TOKEN"] == secret
        assert launch.secret_values == [secret]
        assert "ANTHROPIC_API_KEY" not in launch.env
        assert "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR" not in launch.env
        assert "YUKI_MODEL_API_KEY" not in launch.env
        config = _settings(launch)
        assert config["forceLoginMethod"] == "claudeai"
        assert config["apiKeyHelper"] == ""
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in config["env"]
        assert "-r" in launch.argv
        assert secret not in repr(launch)
        assert secret not in " ".join(launch.argv)
        for path in launch.temp_files:
            assert secret not in Path(path).read_text(encoding="utf-8")
        assert dict(os.environ) == parent
    finally:
        _cleanup(launch)
    launch = _launch(ClaudeCodeHarness(), "same-gh")
    try:
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in launch.env
        assert _settings(launch)["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    finally:
        _cleanup(launch)


@pytest.mark.parametrize("contents", [None, "", " \n", "first\nsecond", "first\0second", b"\xff"])
def test_invalid_subscription_token_fails_before_launch(subscription_token, contents):
    if contents is None:
        subscription_token.unlink()
    elif isinstance(contents, bytes):
        subscription_token.write_bytes(contents)
    else:
        subscription_token.write_text(contents)
    # Pickers must not read credentials.
    assert ClaudeCodeHarness().models()
    with patch("subprocess.Popen") as popen:
        result = run_agent(ClaudeCodeHarness(), "hello", model="sonnet")
    popen.assert_not_called()
    assert result.is_error and "token" in result.text.lower()
    assert "first" not in result.text


def test_subscription_token_is_reread_each_launch(subscription_token):
    subscription_token.write_text("rotated-token\n")
    launch = _launch(ClaudeCodeHarness(), "sonnet")
    try:
        assert launch.env["CLAUDE_CODE_OAUTH_TOKEN"] == "rotated-token"
    finally:
        _cleanup(launch)


@pytest.mark.parametrize("harness,connection,token", [
    ("claude-code", "gh", {"file": "token"}),
    ("codex", "subscription", {"file": "token"}),
    ("claude-code", "subscription", "plaintext-secret"),
    ("claude-code", "subscription", {"env": "TOKEN"}),
    ("claude-code", "subscription", {"file": ""}),
])
def test_invalid_subscription_token_config(endpoints, harness, connection, token):
    data = yaml.safe_load(endpoints.read_text())
    section = data["harness"][harness]
    if connection == "subscription":
        alias = "sonnet" if harness == "claude-code" else "sol"
        connection = section["aliases"][alias]["connection"]
    section["connections"][connection]["oauth_token"] = token
    endpoints.write_text(yaml.safe_dump(data))
    with pytest.raises(ModelConfigError) as error:
        load_harness_models(harness)
    assert "plaintext-secret" not in str(error.value)


def test_listing_does_not_read_keys_but_launch_requires_them(endpoints, monkeypatch):
    monkeypatch.delenv("ROUTE_GH")
    assert CodexHarness().models()
    with patch("subprocess.Popen") as popen:
        result = run_agent(CodexHarness(), "hello")
    popen.assert_not_called()
    assert result.is_error and "ROUTE_GH is empty" in result.text


def test_unknown_alias_never_launches_or_falls_back(endpoints):
    with patch("subprocess.Popen") as popen:
        result = run_agent(CodexHarness(), "hello", model="deleted-alias", session_id="s")
    popen.assert_not_called()
    assert result.is_error and "Unknown model alias" in result.text
    assert result.session_id == "s"


@pytest.mark.parametrize("change", [
    {"api_key": "plaintext-secret"},
    {"api_key": {"env": "ROUTE_GH", "file": "green-key"}},
    {"api_key": {"value": "plaintext-secret"}},
    {"base_url": "http://user:plaintext-secret@localhost:4000"},
    {"base_url": "http://localhost:4000?key=plaintext-secret"},
    {"base_url": "file:///tmp/test"},
    {"type": "subscription", "api_key": {"env": "ROUTE_GH"}},
])
def test_invalid_connections_fail_without_echoing_secret_source(endpoints, change):
    data = yaml.safe_load(endpoints.read_text())
    data["harness"]["codex"]["connections"]["gh"].update(change)
    endpoints.write_text(yaml.safe_dump(data))
    with pytest.raises(ModelConfigError) as error:
        load_harness_models("codex")
    assert "plaintext-secret" not in str(error.value)


def test_missing_and_malformed_config_are_actionable_and_do_not_echo_source(endpoints):
    endpoints.write_text('harness: [ plaintext-secret: {')
    with pytest.raises(ModelConfigError, match="models.example.yaml") as error:
        load_harness_models("codex")
    assert "plaintext-secret" not in str(error.value)
    endpoints.unlink()
    with pytest.raises(ModelConfigError, match="models.example.yaml"):
        load_harness_models("codex")


@pytest.mark.parametrize("returncode", [0, 1])
def test_credentials_are_redacted_from_streams_and_results(endpoints, returncode):
    from tests.test_harness_claude_code import _mock_popen, _result_record, _stream

    secret = "gh-test-credential"
    output = _stream(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": secret}]}},
        _result_record(secret),
    )
    events = []
    proc = _mock_popen(stdout=output, stderr=secret, returncode=returncode)
    with patch("subprocess.Popen", return_value=proc):
        result = run_agent(ClaudeCodeHarness(), "hello", on_event=events.append)
    assert secret not in result.text
    assert secret not in repr(events)
    assert "[redacted]" in result.text


@pytest.mark.parametrize("returncode", [0, 1])
def test_subscription_token_is_redacted_from_streams_and_results(subscription_token, returncode):
    from tests.test_harness_claude_code import _mock_popen, _result_record, _stream

    secret = "subscription-test-token"
    output = _stream(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": secret}]}},
        _result_record(secret),
    )
    events = []
    proc = _mock_popen(stdout=output, stderr=secret, returncode=returncode)
    with patch("subprocess.Popen", return_value=proc):
        result = run_agent(ClaudeCodeHarness(), "hello", model="sonnet", on_event=events.append)
    assert secret not in result.text
    assert secret not in repr(events)
    assert "[redacted]" in result.text
