"""Capa de embeddings locales."""

from app.embeddings.bge_m3 import (
    EmbeddingUnavailableError,
    get_embedding_service,
    reset_embedding_service,
)

__all__ = [
    "EmbeddingUnavailableError",
    "get_embedding_service",
    "reset_embedding_service",
]
