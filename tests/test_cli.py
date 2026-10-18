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
