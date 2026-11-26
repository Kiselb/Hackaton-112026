from types import SimpleNamespace

import pytest

from clip_agent.config import Settings
from clip_agent.llm import (
    LlmProviderError,
    LlmSelection,
    LlmSelectionError,
    LlmSession,
    OpenAIProvider,
    llm_capabilities,
    resolve_llm_selection,
)


def configured_settings(**overrides) -> Settings:
    values = {
        "openai_enabled": True,
        "openai_api_key": "test-secret-key",
        "openai_default_model": "gpt-6.2-sol",
        "openai_allowed_models": "gpt-6.2-sol,gpt-6.2-astra",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeResponses:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def completed_response(text: str = "Готово"):
    return SimpleNamespace(
        status="completed",
        output_text=text,
        output=[],
        incomplete_details=None,
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )


def test_request_level_selection_is_independent_and_allowlisted() -> None:
    settings = configured_settings()

    first = resolve_llm_selection("openai", "gpt-6.2-astra", settings=settings)
    second = resolve_llm_selection("ollama", None, settings=settings)

    assert first == LlmSelection("openai", "gpt-6.2-astra")
    assert second == LlmSelection("ollama", settings.ollama_model)
    assert settings.llm_default_provider == "ollama"
    with pytest.raises(LlmSelectionError, match="не разрешена"):
        resolve_llm_selection("openai", "arbitrary-model", settings=settings)


def test_openai_requires_explicit_enablement_and_backend_key() -> None:
    settings = configured_settings(openai_enabled=False, openai_api_key=None)

    with pytest.raises(LlmSelectionError) as error:
        resolve_llm_selection("openai", None, settings=settings)

    assert error.value.status_code == 503
    capabilities = llm_capabilities(settings)
    openai = next(item for item in capabilities["providers"] if item["id"] == "openai")
    assert openai["configured"] is False
    assert "api_key" not in str(capabilities).lower()
    assert "test-secret-key" not in str(capabilities)


def test_openai_responses_request_is_private_bounded_and_usage_is_normalized() -> None:
    settings = configured_settings()
    responses = FakeResponses(completed_response())
    client = SimpleNamespace(responses=responses)
    provider = OpenAIProvider(LlmSelection("openai", "gpt-6.2-sol"), settings, client)

    text, usage = provider.generate(
        "Return JSON only",
        json_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        schema_name="check",
    )

    assert text == "Готово"
    assert usage["prompt_eval_count"] == 11
    assert usage["eval_count"] == 7
    request = responses.requests[0]
    assert request["store"] is False
    assert request["max_output_tokens"] == settings.openai_max_output_tokens
    assert request["reasoning"] == {"effort": "medium"}
    assert request["text"]["format"]["type"] == "json_schema"
    assert "tools" not in request
    assert "temperature" not in request
    assert "test-secret-key" not in str(request)


@pytest.mark.parametrize(
    ("response", "category"),
    [
        (
            SimpleNamespace(
                status="incomplete",
                output_text="",
                output=[],
                incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                usage=SimpleNamespace(input_tokens=10, output_tokens=64),
            ),
            "output_limit",
        ),
        (completed_response(""), "empty_response"),
        (
            SimpleNamespace(
                status="completed",
                output_text="",
                output=[SimpleNamespace(content=[SimpleNamespace(type="refusal")])],
                incomplete_details=None,
                usage=SimpleNamespace(input_tokens=10, output_tokens=2),
            ),
            "refusal",
        ),
    ],
)
def test_openai_incomplete_empty_and_refusal_fail_closed(response, category) -> None:
    settings = configured_settings()
    provider = OpenAIProvider(
        LlmSelection("openai", "gpt-6.2-sol"),
        settings,
        SimpleNamespace(responses=FakeResponses(response)),
    )

    with pytest.raises(LlmProviderError) as error:
        provider.generate("test")

    assert error.value.category == category
    assert error.value.usage["prompt_eval_count"] > 0


def test_provider_failure_does_not_fallback_and_is_safely_classified() -> None:
    class AuthenticationError(Exception):
        pass

    settings = configured_settings()
    provider = OpenAIProvider(
        LlmSelection("openai", "gpt-6.2-sol"),
        settings,
        SimpleNamespace(responses=FakeResponses(error=AuthenticationError("secret detail"))),
    )
    session = LlmSession(
        LlmSelection("openai", "gpt-6.2-sol"), settings=settings, provider=provider
    )

    with pytest.raises(LlmProviderError) as error:
        session.generate("test")

    assert error.value.category == "authentication"
    assert "secret detail" not in str(error.value)
    assert session.calls == 1
    assert session.summary()["provider_error_category"] == "authentication"


def test_incomplete_response_usage_is_still_counted() -> None:
    settings = configured_settings()
    response = SimpleNamespace(
        status="incomplete",
        output_text="",
        output=[],
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        usage=SimpleNamespace(input_tokens=20, output_tokens=64),
    )
    provider = OpenAIProvider(
        LlmSelection("openai", "gpt-6.2-sol"),
        settings,
        SimpleNamespace(responses=FakeResponses(response)),
    )
    session = LlmSession(
        LlmSelection("openai", "gpt-6.2-sol"), settings=settings, provider=provider
    )

    with pytest.raises(LlmProviderError, match="незавершённый"):
        session.generate("test")

    assert session.input_tokens == 20
    assert session.output_tokens == 64


def test_readyz_remains_ready_for_ollama_only_without_openai_key(monkeypatch) -> None:
    from clip_agent import main

    class Store:
        def execute(self, statement) -> None:
            del statement

    monkeypatch.setattr(main.settings, "llm_default_provider", "ollama")
    monkeypatch.setattr(main.settings, "openai_enabled", False)
    monkeypatch.setattr(main.settings, "openai_api_key", None)
    monkeypatch.setattr(main, "check_ollama_model", lambda: None)
    monkeypatch.setattr(
        main, "qdrant", lambda: SimpleNamespace(get_collections=lambda: None)
    )
    monkeypatch.setattr(
        main,
        "opensearch",
        lambda: SimpleNamespace(cluster=SimpleNamespace(health=lambda: None)),
    )

    result = main.readyz(Store())

    assert result["status"] == "ready"
    assert result["checks"]["llm_providers"] == {
        "openai": "disabled",
        "ollama": "ok",
    }
