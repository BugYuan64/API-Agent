import io
import json
import logging

import pytest


def render_record(message, *args, context=None, exc_info=None):
    from api_agent.infrastructure.logging import RedactingJsonFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingJsonFormatter(secrets=["fixture-model-key"]))
    logger = logging.Logger("api_agent.test")
    logger.addHandler(handler)
    logger.info(message, *args, extra={"context": context}, exc_info=exc_info)
    return json.loads(stream.getvalue())


def test_logs_redact_known_secrets_headers_and_nested_fields_without_mutation():
    context = {
        "Authorization": "Bearer fixture-target-token",
        "request": {"api_key": "fixture-api-key", "password": "fixture-password"},
        "items": [{"Cookie": "session=fixture-cookie"}],
        "count": 2,
    }
    result = render_record("model %s", "fixture-model-key", context=context)
    serialized = json.dumps(result)
    for secret in [
        "fixture-model-key",
        "fixture-target-token",
        "fixture-api-key",
        "fixture-password",
        "fixture-cookie",
    ]:
        assert secret not in serialized
    assert result["context"]["count"] == 2
    assert context["request"]["password"] == "fixture-password"


def test_logs_redact_inline_credentials_and_exception_text():
    try:
        raise ValueError("bad model credential fixture-model-key")
    except ValueError:
        result = render_record(
            "Authorization: Bearer fixture-token; api_key=fixture-key; "
            "Cookie: session=fixture-cookie; url=https://user:fixture-pass@example.com",
            exc_info=True,
        )
    serialized = json.dumps(result)
    for secret in [
        "fixture-model-key",
        "fixture-token",
        "fixture-key",
        "fixture-cookie",
        "fixture-pass",
    ]:
        assert secret not in serialized
    assert "ValueError" in result["exception"]
    assert result["level"] == "INFO"


def test_cookie_header_hides_all_cookie_values():
    result = render_record("Cookie: session=fixture-first; csrf=fixture-second")
    assert "fixture-first" not in result["message"]
    assert "fixture-second" not in result["message"]


def test_formatted_structured_arguments_and_quoted_keys_are_redacted():
    result = render_record("received %s", {"password": "fixture-argument", "count": 2})
    assert "fixture-argument" not in result["message"]
    result = render_record('{"password": "fixture-json-password", "count": 2}')
    assert "fixture-json-password" not in result["message"]


@pytest.mark.parametrize("secret", ['fixture-prefix"fixture-tail', "fixture-prefix\\fixture-tail"])
def test_json_credentials_with_escaped_characters_are_fully_redacted(secret):
    result = render_record(json.dumps({"password": secret, "count": 2}))
    assert "fixture-prefix" not in result["message"]
    assert "fixture-tail" not in result["message"]
    assert "count" in result["message"]


def test_escaped_json_credentials_in_exception_text_are_fully_redacted():
    try:
        raise ValueError(json.dumps({"password": 'fixture-prefix"fixture-tail'}))
    except ValueError:
        result = render_record("action failed", exc_info=True)
    assert "fixture-prefix" not in result["exception"]
    assert "fixture-tail" not in result["exception"]


def test_numeric_secret_mapping_cannot_trigger_raw_logging_error(capsys):
    result = render_record("token=%(token)d", {"token": 84726391})
    assert "84726391" not in json.dumps(result)
    assert "84726391" not in capsys.readouterr().err
    assert result["level"] == "INFO"
