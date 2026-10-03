"""Superficie pública de indexación RAG."""

from app.rag.indexing import (
    INDEX_CONTRACT_VERSION,
    IndexingError,
    IndexingInvariantError,
    IndexingResult,
    IndexingService,
    derive_fragment_id,
)
from app.rag.lexical import (
    LEXICAL_CONTRACT_VERSION,
    LexicalTermFrequency,
    lexical_term_frequencies,
)

__all__ = [
    "INDEX_CONTRACT_VERSION",
    "LEXICAL_CONTRACT_VERSION",
    "IndexingError",
    "IndexingInvariantError",
    "IndexingResult",
    "IndexingService",
    "LexicalTermFrequency",
    "derive_fragment_id",
    "lexical_term_frequencies",
]
