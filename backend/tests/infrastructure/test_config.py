from pathlib import Path

import pytest


def load_settings(**kwargs):
    from api_agent.infrastructure.config import load_settings as load

    return load(**kwargs)


def test_defaults_need_no_key_and_paths_do_not_follow_shell_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = load_settings(env_file=None)
    assert settings.llm_mode == "openai"
    assert settings.llm_api_key is None
    assert settings.database_path == settings.project_root / "data/api_agent.db"
    assert settings.artifact_dir == settings.project_root / "data/artifacts"
    assert not (tmp_path / "data").exists()
    assert load_settings(env_file=None, llm_mode="disabled").llm_mode == "disabled"


def test_env_file_and_process_override_and_secret_repr(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_NAME=From file\nLLM_MODE=mock\nLLM_API_KEY=fixture-key-value\n", encoding="utf-8"
    )
    monkeypatch.setenv("APP_NAME", "From process")
    settings = load_settings(env_file=env_file)
    assert settings.app_name == "From process"
    assert settings.llm_mode == "mock"
    assert settings.llm_api_key.get_secret_value() == "fixture-key-value"
    assert "fixture-key-value" not in repr(settings)
    assert "fixture-key-value" not in settings.model_dump_json()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("LLM_MODE", "invalid-secret-value"),
        ("LLM_MODE", "live"),
        ("LOG_LEVEL", "invalid-secret-value"),
        ("DATABASE_URL", "postgresql://user:invalid-secret-value@localhost/db"),
        ("DATABASE_URL", "sqlite:///:memory:"),
        ("DATABASE_URL", "sqlite:///"),
        ("ARTIFACT_DIR", ""),
        ("TARGET_ALLOWED_ORIGINS", '["https://user:invalid-secret-value@example.com"]'),
        ("TARGET_ALLOWED_ORIGINS", '["https://example.com/api"]'),
        ("TARGET_ALLOWED_ORIGINS", '["https://example.com:invalid-secret-value"]'),
        ("TARGET_ALLOWED_ORIGINS", '["https://[invalid-secret-value]"]'),
        ("TARGET_ALLOWED_ORIGINS", '["https://invalid-secret-value／example.com"]'),
        ("TARGET_ALLOWED_ORIGINS", '["https://example.com\\u0000"]'),
        ("TARGET_ALLOWED_ORIGINS", "invalid-secret-value"),
    ],
)
def test_invalid_config_names_field_without_echoing_value(field, value, monkeypatch):
    from api_agent.infrastructure.config import ConfigurationError

    monkeypatch.setenv(field, value)
    with pytest.raises(ConfigurationError) as error:
        load_settings(env_file=None)
    assert field in str(error.value)
    assert "invalid-secret-value" not in str(error.value)
    assert error.value.__suppress_context__


def test_explicit_project_root_and_allowed_origins(tmp_path):
    settings = load_settings(
        env_file=None,
        project_root=tmp_path,
        database_url="sqlite:///local/state.db",
        artifact_dir=Path("evidence"),
        target_allowed_origins=["http://127.0.0.1:9000", "https://example.com/"],
    )
    assert settings.database_path == tmp_path / "local/state.db"
    assert settings.artifact_dir == tmp_path / "evidence"
    assert settings.target_allowed_origins == ["http://127.0.0.1:9000", "https://example.com"]


def test_trust_system_proxy_defaults_false_and_controls_postman_env(tmp_path, monkeypatch):
    from api_agent.execution.postman import process_environment

    assert load_settings(env_file=None).trust_system_proxy is False
    monkeypatch.setenv("TRUST_SYSTEM_PROXY", "true")
    assert load_settings(env_file=None).trust_system_proxy is True

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    isolated = process_environment(tmp_path, trust_env=False)
    assert isolated["NO_PROXY"] == "*"
    assert "HTTP_PROXY" not in isolated

    proxied = process_environment(tmp_path, trust_env=True)
    assert proxied["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert proxied["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert proxied.get("NO_PROXY") != "*"
