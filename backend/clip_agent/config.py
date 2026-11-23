from functools import lru_cache
from pathlib import Path

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
    embedding_model: str = "BAAI/bge-m3"
    embedding_device: str = "cpu"
    storage_root: Path = Path("storage")
    frontend_origin: str = "http://127.0.0.1:5173"


@lru_cache
def get_settings() -> Settings:
    return Settings()
