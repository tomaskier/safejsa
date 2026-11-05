from pathlib import Path

import pytest

from safejsa.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_missing_file_gives_defaults(tmp_path):
    config = load_config(tmp_path / "absent.yaml", env={})
    assert config.llm.base_url == "http://localhost:11434/v1"
    assert config.llm.model == "qwen3.5:4b"
    assert config.harness == "v1"
    assert config.email.app_password.get_secret_value() == ""


def test_example_file_loads():
    config = load_config(ROOT / "config.example.yaml", env={})
    assert config.best_harness == "v1"
    assert config.email.allowed_senders == ["you@example.com"]
    assert config.email.poll_seconds == 120


def test_env_overrides_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("llm:\n  model: qwen3.5:4b\nharness: v1\n", encoding="utf-8")
    env = {
        "SAFEJSA_LLM__MODEL": "qwen3.5:9b",
        "SAFEJSA_HARNESS": "v2",
        "SAFEJSA_EMAIL__ALLOWED_SENDERS": "a@example.com, b@example.com",
        "SAFEJSA_EMAIL__POLL_SECONDS": "30",
        "OTHER_VARIABLE": "ignored",
    }
    config = load_config(path, env=env)
    assert config.llm.model == "qwen3.5:9b"
    assert config.harness == "v2"
    assert config.email.allowed_senders == ["a@example.com", "b@example.com"]
    assert config.email.poll_seconds == 30


def test_config_env_variable_selects_file(tmp_path):
    path = tmp_path / "other.yaml"
    path.write_text("harness: v3\n", encoding="utf-8")
    assert load_config(env={"SAFEJSA_CONFIG": str(path)}).harness == "v3"


def test_secrets_are_hidden_in_repr(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("email:\n  app_password: hunter2-secret\n", encoding="utf-8")
    config = load_config(path, env={"SAFEJSA_LANGSMITH__API_KEY": "ls-secret"})
    assert "hunter2-secret" not in repr(config)
    assert "ls-secret" not in str(config.model_dump())
    assert config.email.app_password.get_secret_value() == "hunter2-secret"


@pytest.mark.parametrize(
    "content",
    ["llm:\n  modle: typo\n", "- a\n- b\n", "llm: [unclosed\n"],
    ids=["unknown-key", "not-a-mapping", "bad-yaml"],
)
def test_invalid_file_is_rejected(tmp_path, content):
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path, env={})
