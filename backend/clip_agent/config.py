from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_dsn: str = "postgresql+psycopg://clip_agent:CHANGE_ME@127.0.0.1:5432/clip_agent_2026"
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_collection: str = "clip_agent_dense_v1"
    opensearch_url: str = "http://127.0.0.1:9200"
    opensearch_index: str = "clip_agent_sparse_v1"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3.5:9b"
    ollama_think: bool = False
    ollama_temperature: float = 0.0
    ollama_num_predict: int = 512
    ollama_timeout_seconds: float = Field(default=300.0, gt=0, le=600)
    llm_default_provider: Literal["ollama", "openai"] = "ollama"
    llm_max_total_tokens_per_request: int = Field(default=12000, ge=256, le=1_000_000)
    openai_enabled: bool = False
    openai_api_key: SecretStr | None = None
    openai_default_model: str = "gpt-6.2-sol"
    openai_allowed_models: str = "gpt-6.2-sol,gpt-6.2-astra"
    openai_timeout_seconds: float = Field(default=45.0, gt=0, le=300)
    openai_max_output_tokens: int = Field(default=2048, ge=64, le=128000)
    openai_reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    embedding_model: str = "BAAI/bge-m3"
    embedding_device: str = "cpu"
    rerank_enabled: bool = False
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_model_revision: str | None = None
    rerank_device: str = "cpu"
    rerank_candidates: int = Field(default=24, ge=1)
    rerank_top_k: int = Field(default=8, ge=1)
    agent_enabled: bool = True
    agent_max_search_calls: int = Field(default=3, ge=1, le=10)
    agent_max_retries: int = Field(default=1, ge=0, le=3)
    agent_max_llm_calls: int = Field(default=4, ge=1, le=10)
    agent_assessment_max_output_tokens: int = Field(default=2048, ge=128, le=8192)
    agent_composition_max_output_tokens: int = Field(default=1024, ge=128, le=8192)
    agent_validation_max_output_tokens: int = Field(default=512, ge=128, le=4096)
    agent_context_window_tokens: int = Field(default=12288, ge=4096, le=131072)
    multi_query_enabled: bool = True
    multi_query_default_enabled: bool = False
    multi_query_max_variants: int = Field(default=2, ge=1, le=5)
    glossary_enabled: bool = False
    glossary_path: Path = Path("storage/glossary/customer_v1.json")
    agent_demo_override_enabled: bool = False
    conversation_ttl_minutes: int = Field(default=60, ge=5, le=1440)
    conversation_max_clarifications: int = Field(default=2, ge=1, le=5)
    conversation_max_user_answers: int = Field(default=3, ge=1, le=10)
    conversation_max_context_chars: int = Field(default=2000, ge=256, le=8000)
    conversation_max_message_chars: int = Field(default=1000, ge=64, le=4000)
    quality_email_enabled: bool = False
    quality_email_to: str = ""
    quality_smtp_host: str = ""
    quality_smtp_port: int = Field(default=587, ge=1, le=65535)
    quality_smtp_username: str = ""
    quality_smtp_password: SecretStr | None = None
    quality_smtp_from: str = ""
    quality_smtp_starttls: bool = True
    quality_smtp_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    quality_email_max_body_chars: int = Field(default=8000, ge=1000, le=20000)
    quality_request_rate_limit_per_hour: int = Field(default=10, ge=1, le=100)
    quality_approval_token: SecretStr | None = None
    public_demo_enabled: bool = False
    demo_max_concurrent_llm: int = Field(default=1, ge=1, le=16)
    demo_max_concurrent_ingest: int = Field(default=1, ge=1, le=8)
    demo_max_upload_mb: int = Field(default=25, ge=1, le=100)
    storage_root: Path = Path("storage")
    frontend_origin: str = "http://127.0.0.1:5173"


@lru_cache
def get_settings() -> Settings:
    return Settings()
