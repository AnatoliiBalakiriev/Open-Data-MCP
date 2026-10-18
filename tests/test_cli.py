from __future__ import annotations

from open_data_mcp import cli


def test_ask_without_api_key_explains_setup(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["ask", "hello"]) == 2
    assert ".env.example" in capsys.readouterr().err


def test_dotenv_does_not_override_existing_variables(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("# c\nFOO_TEST_A=from_file\nFOO_TEST_B='quoted'\nbad line\n", "utf-8")
    monkeypatch.setenv("FOO_TEST_A", "already")
    monkeypatch.delenv("FOO_TEST_B", raising=False)
    cli.load_dotenv(env)
    import os

    assert os.environ["FOO_TEST_A"] == "already" and os.environ["FOO_TEST_B"] == "quoted"
    monkeypatch.delenv("FOO_TEST_B")


def test_default_model_is_the_verified_sonnet_id():
    from open_data_mcp.agent import DEFAULT_MODEL

    assert DEFAULT_MODEL == "claude-sonnet-5-5"


def test_server_subprocess_gets_proxy_and_portal_settings_but_never_the_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:3128")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/ca.pem")
    monkeypatch.setenv("OPEN_DATA_PORTAL_URL", "https://example.test")
    monkeypatch.setenv("UNRELATED_SECRET", "x")
    env = cli._server_params(offline=False).env or {}
    assert env["HTTPS_PROXY"] == "http://proxy.test:3128"
    assert env["SSL_CERT_FILE"] == "/tmp/ca.pem"
    assert env["OPEN_DATA_PORTAL_URL"] == "https://example.test"
    assert "ANTHROPIC_API_KEY" not in env and "UNRELATED_SECRET" not in env
