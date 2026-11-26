from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from .config import Settings, get_settings


ProviderName = Literal["ollama", "openai"]


@dataclass(frozen=True)
class LlmSelection:
    provider: ProviderName
    model: str


class LlmSelectionError(ValueError):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class LlmProviderError(RuntimeError):
    """Safe provider failure suitable for traces and controlled responses."""

    def __init__(
        self,
        category: str,
        message: str,
        *,
        usage: dict[str, Any] | None = None,
        attempted: bool = True,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.usage = usage or {}
        self.attempted = attempted


def _csv_models(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def openai_allowed_models(settings: Settings | None = None) -> tuple[str, ...]:
    configured = settings or get_settings()
    return _csv_models(configured.openai_allowed_models)


def openai_is_configured(settings: Settings | None = None) -> bool:
    configured = settings or get_settings()
    key = configured.openai_api_key.get_secret_value() if configured.openai_api_key else ""
    return configured.openai_enabled and bool(key.strip())


def resolve_llm_selection(
    provider: str | None,
    model: str | None,
    *,
    settings: Settings | None = None,
) -> LlmSelection:
    configured = settings or get_settings()
    selected_provider = provider or configured.llm_default_provider
    if selected_provider not in {"ollama", "openai"}:
        raise LlmSelectionError("Неизвестный LLM-провайдер.", status_code=422)

    if selected_provider == "ollama":
        selected_model = model or configured.ollama_model
        if selected_model != configured.ollama_model:
            raise LlmSelectionError("Выбранная модель Ollama не разрешена сервером.", status_code=422)
        return LlmSelection(provider="ollama", model=selected_model)

    allowed = openai_allowed_models(configured)
    selected_model = model or configured.openai_default_model
    if selected_model not in allowed:
        raise LlmSelectionError("Выбранная модель OpenAI не разрешена сервером.", status_code=422)
    if not configured.openai_enabled:
        raise LlmSelectionError("Провайдер OpenAI отключён на сервере.", status_code=503)
    if not openai_is_configured(configured):
        raise LlmSelectionError("Провайдер OpenAI не настроен на сервере.", status_code=503)
    return LlmSelection(provider="openai", model=selected_model)


def llm_capabilities(settings: Settings | None = None) -> dict[str, Any]:
    configured = settings or get_settings()
    allowed = openai_allowed_models(configured)
    return {
        "default_provider": configured.llm_default_provider,
        "providers": [
            {
                "id": "ollama",
                "label": "Ollama",
                "enabled": True,
                "configured": True,
                "available": True,
                "default_model": configured.ollama_model,
                "models": [configured.ollama_model],
            },
            {
                "id": "openai",
                "label": "OpenAI",
                "enabled": configured.openai_enabled,
                "configured": openai_is_configured(configured),
                "available": openai_is_configured(configured),
                "default_model": configured.openai_default_model,
                "models": list(allowed),
            },
        ],
    }


class OllamaProvider:
    def __init__(self, selection: LlmSelection, settings: Settings) -> None:
        self.selection = selection
        self.settings = settings

    def generate(
        self,
        prompt: str,
        *,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "structured_response",
        max_output_tokens: int | None = None,
        context_window_tokens: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        del schema_name
        output_limit = max_output_tokens or self.settings.ollama_num_predict
        payload: dict[str, Any] = {
            "model": self.selection.model,
            "stream": False,
            "think": self.settings.ollama_think,
            "messages": [{"role": "user", "content": prompt}],
            "options": {
                "temperature": self.settings.ollama_temperature,
                "num_predict": output_limit,
            },
        }
        if json_schema is not None:
            payload["format"] = json_schema
        if context_window_tokens is not None:
            payload["options"]["num_ctx"] = context_window_tokens
        started = time.perf_counter()
        try:
            response = httpx.post(
                f"{self.settings.ollama_url}/api/chat",
                json=payload,
                timeout=self.settings.ollama_timeout_seconds,
            )
            response.raise_for_status()
            usage = response.json()
            content = usage["message"]["content"].strip()
        except httpx.TimeoutException as exc:
            raise LlmProviderError("timeout", "LLM-провайдер не ответил вовремя.") from exc
        except httpx.RequestError as exc:
            raise LlmProviderError("network", "LLM-провайдер временно недоступен.") from exc
        except httpx.HTTPStatusError as exc:
            category = "server_error" if exc.response.status_code >= 500 else "request_rejected"
            raise LlmProviderError(category, "LLM-провайдер отклонил запрос.") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise LlmProviderError("malformed_response", "LLM-провайдер вернул неверный ответ.") from exc
        normalized = {
            **usage,
            "provider": self.selection.provider,
            "model": self.selection.model,
            "provider_latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "response_chars": len(content),
            "json_schema_requested": json_schema is not None,
            "max_output_tokens": output_limit,
            "context_window_tokens": context_window_tokens,
            "output_limit_reached": usage.get("done_reason") == "length",
        }
        if normalized["output_limit_reached"] and json_schema is not None:
            raise LlmProviderError(
                "output_limit",
                "Ollama остановил генерацию по лимиту output tokens.",
                usage=normalized,
            )
        if not content:
            raise LlmProviderError(
                "empty_response",
                "LLM-провайдер не вернул итоговый текст.",
                usage=normalized,
            )
        return content, normalized


class OpenAIProvider:
    def __init__(self, selection: LlmSelection, settings: Settings, client: Any = None) -> None:
        self.selection = selection
        self.settings = settings
        self._client = client

    def _client_instance(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.settings.openai_api_key.get_secret_value(),
                timeout=self.settings.openai_timeout_seconds,
                max_retries=0,
            )
        return self._client

    @staticmethod
    def _exception_category(exc: Exception) -> str:
        name = type(exc).__name__
        if name == "AuthenticationError":
            return "authentication"
        if name == "RateLimitError":
            return "rate_limit"
        if name == "APITimeoutError":
            return "timeout"
        if name == "APIConnectionError":
            return "network"
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int):
            if status_code == 401:
                return "authentication"
            if status_code == 429:
                return "rate_limit"
            if status_code >= 500:
                return "server_error"
            return "request_rejected"
        if name == "APIStatusError":
            return "request_rejected"
        return "provider_error"

    @staticmethod
    def _contains_refusal(response: Any) -> bool:
        for item in getattr(response, "output", []) or []:
            for content in getattr(item, "content", []) or []:
                if getattr(content, "type", None) == "refusal":
                    return True
        return False

    def generate(
        self,
        prompt: str,
        *,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "structured_response",
        max_output_tokens: int | None = None,
        context_window_tokens: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        del context_window_tokens
        output_limit = max_output_tokens or self.settings.openai_max_output_tokens
        request: dict[str, Any] = {
            "model": self.selection.model,
            "input": prompt,
            "store": False,
            "max_output_tokens": output_limit,
            "reasoning": {"effort": self.settings.openai_reasoning_effort},
        }
        if json_schema is not None:
            request["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": json_schema,
                    "strict": True,
                }
            }
        started = time.perf_counter()
        try:
            response = self._client_instance().responses.create(**request)
        except Exception as exc:
            category = self._exception_category(exc)
            raise LlmProviderError(category, "OpenAI API недоступен для этого запроса.") from exc

        usage = getattr(response, "usage", None)
        normalized = {
            "prompt_eval_count": int(getattr(usage, "input_tokens", 0) or 0),
            "eval_count": int(getattr(usage, "output_tokens", 0) or 0),
            "done_reason": getattr(response, "status", None),
            "provider": self.selection.provider,
            "model": self.selection.model,
            "provider_latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "response_chars": len(str(getattr(response, "output_text", "") or "").strip()),
            "json_schema_requested": json_schema is not None,
            "max_output_tokens": output_limit,
            "output_limit_reached": False,
        }
        if self._contains_refusal(response):
            raise LlmProviderError(
                "refusal", "OpenAI не смог предоставить ответ.", usage=normalized
            )
        if getattr(response, "status", None) != "completed":
            details = getattr(response, "incomplete_details", None)
            reason = getattr(details, "reason", None)
            category = "output_limit" if reason == "max_output_tokens" else "incomplete_response"
            normalized["output_limit_reached"] = category == "output_limit"
            raise LlmProviderError(
                category, "OpenAI вернул незавершённый ответ.", usage=normalized
            )
        content = str(getattr(response, "output_text", "") or "").strip()
        if not content:
            raise LlmProviderError(
                "empty_response",
                "OpenAI не вернул итоговый текст в пределах output budget.",
                usage=normalized,
            )
        return content, normalized


class LlmSession:
    """One request-scoped provider/model and cumulative token budget."""

    def __init__(
        self,
        selection: LlmSelection,
        *,
        settings: Settings | None = None,
        provider: OllamaProvider | OpenAIProvider | None = None,
    ) -> None:
        self.selection = selection
        self.settings = settings or get_settings()
        self.provider = provider or (
            OpenAIProvider(selection, self.settings)
            if selection.provider == "openai"
            else OllamaProvider(selection, self.settings)
        )
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.latency_ms = 0.0
        self.last_error_category: str | None = None

    def generate(
        self,
        prompt: str,
        *,
        json_schema: dict[str, Any] | None = None,
        schema_name: str = "structured_response",
        max_output_tokens: int | None = None,
        context_window_tokens: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        remaining = (
            self.settings.llm_max_total_tokens_per_request
            - self.input_tokens
            - self.output_tokens
        )
        if remaining <= 0:
            self.last_error_category = "token_budget_exceeded"
            raise LlmProviderError(
                "token_budget_exceeded",
                "Лимит токенов для одного запроса исчерпан.",
                attempted=False,
            )
        provider_default = (
            self.settings.openai_max_output_tokens
            if self.selection.provider == "openai"
            else self.settings.ollama_num_predict
        )
        effective_output_limit = min(max_output_tokens or provider_default, remaining)
        self.calls += 1
        try:
            content, usage = self.provider.generate(
                prompt,
                json_schema=json_schema,
                schema_name=schema_name,
                max_output_tokens=effective_output_limit,
                context_window_tokens=context_window_tokens,
            )
        except LlmProviderError as exc:
            self.input_tokens += int(exc.usage.get("prompt_eval_count") or 0)
            self.output_tokens += int(exc.usage.get("eval_count") or 0)
            self.latency_ms += float(exc.usage.get("provider_latency_ms") or 0)
            self.last_error_category = exc.category
            raise
        self.input_tokens += int(usage.get("prompt_eval_count") or 0)
        self.output_tokens += int(usage.get("eval_count") or 0)
        self.latency_ms += float(usage.get("provider_latency_ms") or 0)
        if self.input_tokens + self.output_tokens > self.settings.llm_max_total_tokens_per_request:
            self.last_error_category = "token_budget_exceeded"
            raise LlmProviderError(
                "token_budget_exceeded",
                "Лимит токенов для одного запроса превышен.",
                usage=usage,
            )
        return content, usage

    def __call__(self, prompt: str) -> tuple[str, dict[str, Any]]:
        return self.generate(prompt)

    def summary(self) -> dict[str, Any]:
        return {
            "llm_provider": self.selection.provider,
            "llm_model": self.selection.model,
            "llm_calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "provider_latency_ms": round(self.latency_ms, 3),
            "provider_error_category": self.last_error_category,
        }


def create_llm_session(
    selection: LlmSelection, *, settings: Settings | None = None
) -> LlmSession:
    return LlmSession(selection, settings=settings)
