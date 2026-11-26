import os

import pytest

from clip_agent.config import Settings
from clip_agent.llm import OpenAIProvider, resolve_llm_selection


@pytest.mark.integration
def test_real_openai_responses_minimal() -> None:
    if os.getenv("RUN_OPENAI_INTEGRATION") != "1":
        pytest.skip("set RUN_OPENAI_INTEGRATION=1 for the explicitly paid live test")
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        pytest.skip("OPENAI_API_KEY is not configured")
    model = os.getenv("OPENAI_INTEGRATION_MODEL", "gpt-6.2-sol")
    settings = Settings(
        _env_file=None,
        openai_enabled=True,
        openai_api_key=api_key,
        openai_default_model=model,
        openai_allowed_models=model,
        openai_max_output_tokens=64,
        openai_reasoning_effort="low",
    )
    selection = resolve_llm_selection("openai", model, settings=settings)

    text, usage = OpenAIProvider(selection, settings).generate(
        "Reply with exactly the single word OK."
    )

    assert text
    assert usage["prompt_eval_count"] > 0
    assert usage["eval_count"] > 0
